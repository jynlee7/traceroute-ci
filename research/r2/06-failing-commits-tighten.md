# R2-06: failing commits, selected vs failing vs oracle floor

Method: read-only comparison of `work/out.jsonl` against `sample/answers.json` and `sample/tests.json`. Cost = max worker load + 2 s per worker. The oracle is the failing set alone, split best over 1..8 workers.

## 1. Where the gap is (per commit, simulated seconds)

```
commit kind   fail  sel  cost  oracle  gap   what was over-selected
535z   code     1   171  11.2   2.6    8.6   generator_memory 6.7, annotate_funcs 2.0, canonicalize 1.0 + 165 dialect tests
bj7g   code     1     3   3.6   2.0    1.6   test_qualify_columns 1.4 (it really runs the changed line)
3utz   code    37   181  27.3  25.5    1.8   167 optimizer tests, 11.6 s, but they sit on the 2nd worker beside tpcds
2jrq   code     8    32  25.5  25.5    0     extras (4.5 s) hide behind tpcds 21.5
ary6   code    10    10   4.4   4.4    0
j6uz   code     2     2   4.4   4.4    0
6ze8 / 986c / bvw2 / rszg / all 8 fixture commits: gap 0
TOTAL for failing commits: 12.0 s over the oracle.
```

- **The failing commits are already near-tight.** The sample's two failing-commit levers are `535z` and `bj7g`/`3utz` (about 3.4 s together), and only `535z` is fixable by a general rule.
- **The real gap is in commits that fail nothing.** The 12 no-fail code commits cost 123 s against an oracle of 0. Those are other agents' topics.
- **While tracing 535z I found a root cause that also inflates `fryt`.** It is a bug in `tools/build_index.py`, section 3.

## 2. Case studies

### 535z: tsql.py class attribute `WEEK_OFFSET = -1` -> `-2` (selected 171, 1 fails)

**Rule that fired.** The change is in a class body, outside any function, so `sqlglot_change` goes to `import_time_change` (`selector.py:~420`). Its pieces are:
- `file_cov(family)`: every test that touched any line of tsql.py or fabric.py;
- `name_tests({tsql, fabric})`: test_tsql 65 tests, 39 test_dialect tests, and others;
- `class_tests`;
- `all_dialect_tests`, which adds generator_memory 6.7 s (selector comment: "an edit that breaks a dialect module's import fails it").

**Why that is over-broad.** A literal-for-literal swap (`-1` to `-2`) cannot break an import. The attribute is read in only three places:

```
sqlglot/generator.py:6768         self.dialect.WEEK_OFFSET
sqlglot/dialects/dialect.py:2287  dialect.WEEK_OFFSET
sqlglot/optimizer/simplify.py:545 dialect.WEEK_OFFSET   (the failing test, test_simplify, runs this)
```

**Measured from `maps/impact.json.gz`.** The union of the coverage of those three lines is 10 tests, 0.9 s total, and it **contains the failing test** (`test_simplify`). The 10 tests are:
- test_bigquery::test_week;
- 2 exasol tests;
- 5 oracle tests;
- test_teradata::test_trunc;
- test_optimizer::test_simplify.

Cost would drop from 11.2 to about 3 s: **-8 s on this commit**.

**General principle.** The consumers of a class-level constant are the lines that read its name, and coverage already knows which tests ran them.

### fryt (fails nothing, same root cause family): `_builder` closure edit treated as import-time

`work/why.log` says `dremio.py:sqlglot:import-fn:37`. `work/index.json` has `import_fns["sqlglot/parsers/dremio.py"] = [[38,55,[]], [33,57,[[38,55]]]]`, so the nested closure `_builder` (lines 38-55) is itself listed as an import-time function.

**Cause: `tools/build_index.py:~237`:**
```python
once = once or name.startswith("_build")
```
- Intent: lazy table builders such as `generator._build_dispatch(cls)`.
- Effect: it catches every `_builder` closure and every `_build_<fn>(args)` parser builder. These run per parse, and coverage records them per test.
- Count in the current index: **125 of 360 import-fn entries are `_build*`**, including 25 `_builder` closures (dialect.py x7, bigquery x4, snowflake x4, ...).

**Consequence.** An edit in any of them pulls in `import_time_change`, which means `all_dialect_tests` and generator_memory (6.7 s), plus the whole file coverage of the dialect family.

**Measured effect.** Plain `cov(range(34,59))` of dremio.py selects 7 tests, 0.2 s. fryt would go from 9.2 s to about 2.2 s: **-7 s**.

**Recall impact.** Only the three real lazy builders matter:
- `generator._build_dispatch(cls)`: the only genuinely lazy one, first-use cached in `_DISPATCH_CACHE`;
- `simplify._build_gen_dispatch()` and `annotate_types._build_coerces_to()`: both are called at module level, so the call-seed rule already catches them.

### bj7g (clickhouse `values_as_table = True` -> `False`)

- The extra is `test_qualify_columns` 1.4 s. It executes the changed line (788) itself, and `slow` pruning only applies at >= 5 s.
- **No general rule removes it with coverage alone. Do not try; the 1.6 s is the price of recall.**

### 3utz (serde.py `and` -> `or`)

- The 181 selected tests are the coverage of `serde.dump` line 50, run by 167 optimizer tests. 37 really fail.
- The extras (11.6 s) sit on the second worker beside tpcds 21.5 s, so the real loss is only 1.8 s (27.3 vs 25.5).
- Not worth a rule.

### Already tight

- 2jrq, ary6, j6uz, 6ze8, rszg, 986c, bvw2 and the fixture commits select exactly the failing set plus 0-1 free tests.
- Fixture routing by stem and the ast-identical drop work well.
- The `test_typing.py` addition on typing edits costs 0.0 s.

## 3. Proposed changes (general; all derived from the pinned source and coverage)

### R1. `tools/build_index.py`: stop treating `_build*` closures as run-once (~line 237)

```diff
-                once = once or name.startswith("_build")
+                # Lazy table builders are module-level and take no arguments (or the class, as
+                # generator._build_dispatch(cls)). `_build_<fn>(args)` parser builders and `_builder`
+                # closures run once per parse and coverage records them per test.
+                once = once or (
+                    name.startswith("_build")
+                    and d in tree.body
+                    and [a.arg for a in d.args.args] in ([], ["cls"])
+                )
```
(`d in tree.body` is an identity test on module-level defs, so nested closures and methods fail it.)

No selector change is needed. Sample effect: fryt -7 s, with no other sample effect expected. The hidden set (1.5x larger) probably has several `_builder` / `_build_*` edits, since these are the usual injected-bug sites.

### R2. Literal class-attribute edits use consumer coverage, not the import-time blast (535z, -8 s)

**build_index.py**: add after `import_fns` (it has `trees`, `root`):
```python
def _is_lit(v) -> bool:
    return isinstance(v, ast.Constant) or (
        isinstance(v, ast.UnaryOp) and isinstance(v.op, (ast.USub, ast.UAdd, ast.Not)) and isinstance(v.operand, ast.Constant))

const_attrs: set[str] = set()
for rel, tree in trees.items():
    if rel.startswith(("sqlglot/expressions/", "sqlglot/typing/")):
        continue
    for c in ast.walk(tree):
        if isinstance(c, ast.ClassDef):
            for s in c.body:
                tgt = (s.targets[0] if isinstance(s, ast.Assign) and len(s.targets) == 1
                       else s.target if isinstance(s, ast.AnnAssign) and s.value is not None else None)
                if isinstance(tgt, ast.Name) and _is_lit(s.value) and not tgt.id.startswith("__"):
                    const_attrs.add(tgt.id)

attr_refs: dict[str, list] = {}   # name -> [rel, first line, last line, inside a def/lambda]
def _walk(node, rel, in_def):
    for ch in ast.iter_child_nodes(node):
        d = in_def or isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        name = (ch.attr if isinstance(ch, ast.Attribute) and isinstance(ch.ctx, ast.Load)
                else ch.id if isinstance(ch, ast.Name) and isinstance(ch.ctx, ast.Load)
                else ch.value if isinstance(ch, ast.Constant) and isinstance(ch.value, str) else None)
        if name in const_attrs:                      # string constants cover getattr(x, "NAME")
            attr_refs.setdefault(name, []).append([rel, ch.lineno, ch.end_lineno, d])
        _walk(ch, rel, d)
for rel, tree in trees.items():
    if rel.startswith("sqlglot/"):
        _walk(tree, rel, False)
```
Tests naming the attribute directly: scan `tests/**/*.py` trees the same way (Attribute or str constant in `const_attrs`) into `attr_test_files[name] = [test rel, ...]`.

Write both to the index dict near line 427: `"attr_refs": attr_refs, "attr_test_files": attr_test_files`.

**selector.py**: add two methods to `Selector`, and a hook in `sqlglot_change`.

```python
def const_attr_name(old_tree, new_tree, ln):
    """Name of a class-body `NAME = <literal>` statement on line ln whose new value is also a literal."""
    def lit(v):
        return isinstance(v, ast.Constant) or (isinstance(v, ast.UnaryOp) and isinstance(v.operand, ast.Constant))
    def stmts(tree):
        for c in ast.walk(tree):
            if isinstance(c, ast.ClassDef):
                for s in c.body:
                    tgt = s.targets[0] if isinstance(s, ast.Assign) and len(s.targets) == 1 else (
                        s.target if isinstance(s, ast.AnnAssign) and s.value is not None else None)
                    if isinstance(tgt, ast.Name) and lit(s.value):
                        yield tgt.id, s
    for name, s in stmts(old_tree):
        if s.lineno <= ln <= s.end_lineno and not name.startswith("__"):
            if any(n == name for n, _ in stmts(new_tree)):
                return name
    return None
```
```python
def attr_consumers(self, name: str) -> set[int] | None:
    """Tests that ran any line reading `name`. None: can't tell (no readers, or a reader runs at import)."""
    refs = self.index.get("attr_refs", {}).get(name)
    if not refs:
        return None
    hit: set[int] = set()
    for rel, a, b, in_def in refs:
        if not in_def or self.in_import_fn(rel, a):   # class-body / module-level reader, or lazily run-once
            return None
        hit |= self.cov(rel, range(a, b + 1))
    for tf in self.index.get("attr_test_files", {}).get(name, []):
        hit |= self.by_file.get(tf, set())
    return hit or None
```
Hook in `sqlglot_change`, in the final `else:` branch, immediately before `hit = self.import_time_change(rel, ln, renamed)` (selector.py ~line 474):
```python
name = const_attr_name(old_tree, new_tree, ln) if not rel.endswith("__init__.py") else None
cons = self.attr_consumers(name) if name else None
if cons is not None:
    out |= cons | self.by_file.get(f"tests/dialects/test_{Path(rel).stem}.py", set())
    reasons.append("const-attr")
    continue
```
The dialect's own test file is added as a free safety net: tsql +0.7 s on a worker that already costs 2 s.

Placement matters: the `lambda` branch above keeps running first. Anything that falls back (reader at module level, run-once reader, no readers, non-literal on either side, name starts `__`) takes the old rule unchanged.

### R3 (note only, not recommended yet): dict-entry key narrowing

The gbav shape `"KEY": _show_parser("KEY", target=...)` is a changed entry in a class-body dict. The consumer rule above could be extended: C = coverage of readers of the dict name, intersected with tests whose strings contain the key, case-insensitively. This is riskier, because keys reach the lookup through tries, case-folding and string assembly. `gbav` costs 10.1 s, so it is worth measuring, but I did not validate it.

## 4. Recall analysis for R1 and R2

### R1

- **Miss shape.** A closure named `_build*` that really runs lazily, once per process, with the first test credited. This would be a `functools.cache` or `global` pattern. Those are already caught by the cache-decorator, Global/Nonlocal and `cls.x =` checks that stay in place.
- **Risk: low.**

### R2

Everything below fails safe to the old rule, except possibly the last bullet.

Fall back to the old rule when:
- a reader is outside a def or lambda (class body or module level);
- a reader is in an import-fn (metaclass `klass.X`, `__init_subclass__`, run-once);
- there are no readers;
- either side is non-literal.

Residual miss shapes:
- **Computed names.** `getattr(obj, prefix + "NAME")` or `vars(cls)[...]` loops would miss. I found none for dialect flags in sqlglot (the string-constant scan catches literal getattr).
- **Heritable reads through a different spelling.** For example a copy loop `for k in dir(cls): if k.isupper()`. `_build_dispatch` uses `dir` but only for `_sql` methods. I did not grep every `dir(` / `vars(` use; the implementer should list them and make any whose loop touches constants force a fallback.
- **Coverage gaps.** If a test reads the attribute on a line coverage didn't record (e.g. a line in a C-compiled path), that test is missed.
- **Risk: medium-low**, and the value of the rule is high on hidden commits: any dialect flag flip (`SUPPORTS_*`, `TYPED_DIVISION`, `WEEK_OFFSET`, ...) currently costs about 10-25 s.

### Do not sanity-check by running anything

These were derived from reading `work/index.json` and the coverage map only.

## 5. Ranked recommendations

| # | Change | Sample saving | Hidden-set value | Risk |
|---|--------|---------------|------------------|------|
| 1 | R1: `_build*` run-once only for module-level zero-arg or `cls` builders | 7.0 s (fryt) | high: every `_builder` / `_build_*` edit | low |
| 2 | R2: literal class-attribute edits use reader coverage, not `import_time_change` | 8.6 s (535z) | high: flag flips | medium-low (fallbacks listed) |
| 3 | R3: dict-entry key narrowing (gbav) | up to ~8 s, unvalidated | medium | medium-high |
| 4 | Leave bj7g and 3utz alone | 0 | n/a | n/a |

R1 and R2 together are about 15.6 s on the sample, taking 217.7 s toward ~202 s. They stay at zero misses on the sample: R2's consumer set contains the failing `test_simplify`, and R1 leaves the failing sets of the other commits untouched.

## 6. Summary

1. Failing commits are already near-tight: 12 s over the oracle in total. The 123 s gap on the 12 no-fail commits is where most of the 100 s target lives.
2. 535z's bloat is `import_time_change` (including `all_dialect_tests`, i.e. generator_memory) firing on a literal `-1` to `-2` class-attribute swap.
3. Fix R2: use the coverage of the lines that read the attribute. That is 10 tests, 0.9 s, and it contains the failing `test_simplify`. Saves about 8.6 s, with fallbacks to the old rule for any reader at import time.
4. Bug R1: `build_index.py` `name.startswith("_build")` marks 125 of 360 import-fn entries (25 `_builder` closures included) as run-once. Fixing it saves about 7 s on fryt and helps hidden `_builder` edits.
5. bj7g (1.6 s) and 3utz (1.8 s) are the price of recall. No sound general rule removes them.
