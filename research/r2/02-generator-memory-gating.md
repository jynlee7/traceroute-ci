# r2-02: Gating test_generator_memory, test_max_nodes, test_cache_does_not_retain_queries

Method: I read the test sources, `selector.py`, `tools/build_index.py`, `work/index.json`, `maps/impact.json.gz`, `work/out.jsonl` and the four sample patches, using read-only one-liners. I ran nothing from the repo. Costs below use the scorer's model (LPT over k ≤ 8 workers, max load + 2 s·k), recomputed on `work/out.jsonl` selections.
Round 1 already covers what each test asserts (`research/11-slow-tests.md` F1, F5, F6), so I only restate what the gate depends on.

## (a) Findings

### F1. What test_generator_memory checks (tests/test_generator.py:19-38)
For every `Dialects` member it runs `Dialect.get_or_raise(name)`, makes 100 warm-up `dialect.generator()` calls, then makes 1000 more under `tracemalloc` and asserts that memory grew by less than 1024 B (`assertLess`, line 38).
- It never parses, generates or compares SQL. Its only failure modes are:
  - (i) a per-instance allocation kept alive at module or class level. The places that could do this: `Generator.__init__` (generator.py:950-1009, including `_DISPATCH_CACHE` at 71 and 1005-1008), `_build_dispatch` (74-89), `Dialect.generator` (dialect.py:1378-1380), the subclass `__init__`s (generators/athena.py:137, generators/bigquery.py:704, dialects/athena.py:71), `helper.name_sequence`.
  - (ii) any dialect raising during import, class creation (`_Dialect.__new__`, dialect.py:306-432; `Tokenizer.__init_subclass__`, tokens.py:84-124; the parser metaclass), `Dialect.__init__` (dialect.py:1164) or `Generator.__init__`. An example is `STRING_ESCAPES[0]` (generator.py:~987) on an emptied list.
- Its coverage is 286 lines. Most are import noise: 114 in dialect.py, mostly `_Dialect.__new__`; 37 in tokens.py hooks; 31 in dax files.

### F2. Why it was selected in 4d63, 535z, fryt and gbav
`work/out.jsonl` shows test 1224 in exactly these four commits. All four fail nothing, and 535z fails only test 1434.
- **4d63, 535z, gbav** (`why.log`: `module`): each is a class-body edit in a dialect file. The edit goes to `_import_time_change` (selector.py:393), which adds every `all_dialect_tests` id for any DIALECT_DIRS file (selector.py:403-411). generator_memory is on that list because it names `Dialects` (build_index.py:303-326).
  - 4d63 changes the int `16→17` inside a dict/tuple display (`Tokenizer.NUMERIC_ESCAPES`).
  - 535z changes `WEEK_OFFSET = -1 → -2`, an int under a unary minus.
  - gbav changes `target=True → False`, a keyword argument to `_show_parser(...)` inside the `SHOW_PARSERS` dict display.
  - None of these can raise at import. `_show_parser` (parsers/mysql.py:55-59) only closes over `**kwargs`. `_Dialect.__new__` reads only the *keys* of `NUMERIC_ESCAPES` (dialect.py:~404-411). `WEEK_OFFSET` is not read at import at all. None of them touch generator construction either, so generator_memory cannot fail on any of them.
- **fryt** (`import-fn`): the edit is in the nested `_builder` (parsers/dremio.py:38-55), which only runs while parsing. `index.import_fns["sqlglot/parsers/dremio.py"]` contains `[38, 55, []]`, so `in_import_fn` treats it as running at import, and the edit takes the import-time rule plus all-dialect tests.
  - Cause: **build_index.py:233 `once = once or name.startswith("_build")` also matches `_builder`.**
  - sqlglot has 125 `def _build*`: 25 `_builder` closures and about 95 `_build_xxx(args)` parse-time builders. Only three really run once:
    - `_build_dispatch`, a lazy global cache filled from `Generator.__init__` (generator.py:1005-1008);
    - `_build_gen_dispatch` (simplify.py:1988);
    - `_build_coerces_to` (annotate_types.py:157).
  - The last two are called at module level, so `scan()` already seeds them. The prefix rule is therefore needed only for `_build_dispatch`, and it misfiles about 120 parse-time builders as import-time. Each mis-classified one pulls in the family's whole file coverage plus all-dialect tests whenever it is edited.

### F3. The other all-dialect tests have semantic assertions and must stay
`all_dialect_tests` = test_dialect::test_enum, test_partition_by_excluding, test_druid, test_spark::test_strip_modifiers, test_generator_memory, test_time::test_canonical_time_format_vocabulary.
- An example of why: test_time.py:22-44 asserts on every dialect's `TIME_MAPPING` *values*, so a type-preserving string change in any dialect can fail it.
- So the gate below must apply only to construct-only tests. generator_memory is the only one in that class: it is the only test that uses `tracemalloc` (grep in tests/), and it makes no parse/sql/transpile call.

### F4. test_max_nodes (test_parser.py:1567-1591; 28.8 s) was not selected in any sample commit
- Its inputs are all default-dialect: `SELECT 1`, 500 columns, `x=x…` 10000×100, `x=i`×100, `SELECT 1, 2, 3`.
- Its coverage is 867 lines (parser.py 512, tokenizer_core 210, expressions/core 46, errors 38, tokens 38, dialect.py 18, `__init__` 5) and touches **no dialect-specific file**. So none of the dialect rules can select it today.
- It is exposed in two ways:
  - (a) function edits on hot parser/tokenizer lines. Exact coverage is right here; the median coverage set on its lines is 1368 tests, so those commits run near the full suite anyway.
  - (b) class-body edits to parser.py, tokens.py, dialect.py, expressions/core.py or errors.py. `_import_time_change` adds `file_cov(rel)` (selector.py:397-399), which is roughly the whole suite.
- Full suite cost is 36.8 s; without max_nodes it is 29.5 s. So (b) costs about +7.3 s per commit on top of an already near-full selection.
- An *inert* edit (F5) is not automatically safe for max_nodes. Example: changing the `Tokenizer.SINGLE_TOKENS` key `"="` breaks tokenizing `x=x`. Its gate therefore has to be about which attribute was changed, not about the shape of the edit (R3).

### F5. test_cache_does_not_retain_queries (test_schema.py:406-434; 1.3 s) was not selected in any sample commit
- For `find`, `column_names`, `get_column_type`, `has_column` and `add_table` (each with normalize True and False, dialect duckdb), it asserts that a marker hung on `query.meta` and `table.meta` is garbage-collected after the call. That means no MappingSchema cache holds the Table or query. It also checks `column_names("items") == ["id"]`.
- It fails on:
  - schema.py caching or normalisation changes (179 covered lines);
  - expression `meta`/copy semantics (expressions/core.py);
  - anything that crashes parsing `SELECT id FROM items` in duckdb.
- Its marginal cost is ≤ 1.3 s, so break-even is P(fail) ≈ 0.4 %. The only general cut is the existing sound one: the exact-line pruning at selector.py:512-519 currently applies only to tests ≥ SLOW_MS = 5000. A test that never executed a replaced line cannot observe the replacement, because `same_indent_replacements` excludes continuations and moves. Lowering SLOW_MS to 1000 makes the same rule cover this test, test_scope (2.5), opt test_tpcds (2.2), test_annotate_funcs (2.0) and the rest.

## (b) Savings and recall risk

Sample savings, at the current selections:

| commit | now | without gm | how |
|---|---|---|---|
| 535z | 11.2 | 7.9 | R1 inert gate (−3.3) |
| gbav | 10.1 | 3.4 | R1, using the closure-factory argument rule (−6.7) |
| fryt | 9.2 | 2.5 | R2 `_build` fix (−6.7). R1 does not apply because the edit is in a function |
| 4d63 | 27.5 | 27.5 | 0 today, because tpcds dominates. −1.6 (18.5 → 16.9) once tpcds is gated by another agent |

**Total: about 16.7 s now, about 18.3 s after a tpcds gate.** R2 also shrinks fryt's whole selection, not only gm. max_nodes and the cache test save 0 on the sample. On the hidden set, R3 is worth about 7 s or more per class-body edit to parser.py or tokens.py, which are common commit shapes.

Hidden-commit patch shapes that could cause a miss:
- **R1:** a "type- and truthiness-preserving" constant change that still raises at import. The cases I could find are all excluded by the rule:
  - an int used as an index or `range()` argument at import is excluded, because Subscript/BinOp/Call ancestors are not allowed;
  - a string emptied, where `k[0]` at dialect.py:~395 or `list(...)[0]` at :356 would fail, is excluded by the non-blank rule;
  - a bool flip on an attribute read at import is excluded by the truthiness rule;
  - a constant inside a function body is excluded.
  - Residual risk: a dict-key change that collides with another key and so empties a structure the hooks index into. That needs two keys merging into one where the hook then does `[0]`. Very low.
  - A leak cannot come from a constant at all; it needs a code change in `__init__`, and the fn rule selects that by exact coverage.
- **R2:** a run-once function named `_build*` that is neither seeded nor stores into a module-level dict. Today only `_build_dispatch` exists, and the replacement rule (C2) derives it structurally. Low risk.
- **R3:** an attribute read dynamically (`getattr(self, name)` with a computed name, or the `Dialect.__init__` settings loop), or read in an import hook. Both have fallbacks in C3. Medium risk, so it is ranked below R1 and R2.
- **R4 (SLOW_MS 1000):** the soundness argument already used for ≥ 5 s tests. The only extra exposure is coverage crediting for run-once code, which `import_fns` handles. Low risk.

## (c) Code

### C1. build_index.py: construct-only dialect tests and closure factories
Add the following after the `all_dialect_tests` loop (line ~326). The closure-factory part goes inside the `import_fns` loop, after `defs` is built.

```python
    # Tests that loop over every dialect but only construct objects and measure memory/refs: an
    # edit can fail them only by raising at import/construction or by leaking per instance.
    SQL_CALLS = {"parse_one", "parse", "transpile", "sql", "validate_identity", "validate_all",
                 "tokenize", "optimize", "annotate_types", "qualify", "generate"}
    construct_only_dialect_tests: list[str] = []
    for tid in all_dialect_tests:
        rel, *path = tid.split("::")
        ttree = ast.parse((root / rel).read_text(encoding="utf-8"))
        fn = next((n for n in ast.walk(ttree) if isinstance(n, ast.FunctionDef) and n.name == path[-1]), None)
        if fn is None:
            continue
        names = {getattr(n, "id", None) or getattr(n, "attr", None) for n in ast.walk(fn)}
        calls = {(c.func.attr if isinstance(c.func, ast.Attribute) else getattr(c.func, "id", ""))
                 for c in ast.walk(fn) if isinstance(c, ast.Call)}
        if "tracemalloc" in names and not calls & SQL_CALLS:
            construct_only_dialect_tests.append(tid)
```

Closure factories, inside the `for rel, tree in trees.items()` import-fn loop:
```python
        for name, nodes in defs.items():
            for d in nodes:
                params = {a.arg for a in d.args.args + d.args.kwonlyargs + d.args.posonlyargs}
                params |= {a.arg for a in (d.args.vararg, d.args.kwarg) if a}
                used, todo = False, list(d.body)
                while todo:
                    n = todo.pop()
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                        continue  # runs later, not at import
                    if isinstance(n, ast.Name) and n.id in params:
                        used = True
                    todo.extend(ast.iter_child_nodes(n))
                factory_ok.setdefault(name, True)
                factory_ok[name] &= (not used) and any(isinstance(x, (ast.FunctionDef, ast.Lambda)) for x in ast.walk(d) if x is not d)
```
- Declare `factory_ok: dict[str, bool] = {}` before the loop.
- Emit `"closure_factories": sorted(n for n, ok in factory_ok.items() if ok)` and `"construct_only_dialect_tests": construct_only_dialect_tests` in the dict at line ~428.
- A name defined more than once must be a factory everywhere (the `&=`).

### C2. build_index.py:233: replace the name-prefix rule with a structural one (fixes fryt)
```diff
-                once = once or name.startswith("_build")
+                # (was name.startswith("_build"): also matched ~120 parse-time builders, e.g. `_builder`)
```
Then, after the `defs` loop, seed callees whose result is stored into a module-level container. This matches `dispatch = _build_dispatch(cls); _DISPATCH_CACHE[cls] = dispatch`.
```python
        module_names = {t.id for s in tree.body if isinstance(s, (ast.Assign, ast.AnnAssign))
                        for t in (s.targets if isinstance(s, ast.Assign) else [s.target]) if isinstance(t, ast.Name)}
        for d in (x for ns in defs.values() for x in ns):
            stores = [n for n in ast.walk(d) if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Subscript) and getattr(t.value, "id", None) in module_names for t in n.targets)]
            if not stores:
                continue
            stored = {getattr(s.value, "id", None) for s in stores} - {None}
            for n in ast.walk(d):
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and callee(n.value) and (
                        any(getattr(t, "id", None) in stored for t in n.targets) or n in stores):
                    seeds.add(callee(n.value))
```

### C3. selector.py: the inert-edit gate for construct-only tests (R1)
Add this module-level helper next to `changed_statement_lines` (line ~804):
```python
_ALLOWED_UP = (ast.Dict, ast.List, ast.Tuple, ast.Set, ast.keyword, ast.Assign, ast.AnnAssign, ast.Expr)

def import_inert(old_tree, new_tree, factories: set[str]) -> bool:
    """True when the edit only changes literal constants that no code evaluates at import:
    same AST shape, same constant types and truthiness, non-blank strings, and each changed constant
    reachable from its class/module statement only through displays, unary +/-, a lambda body
    (runs later) or an argument of a closure factory (only captured at import)."""
    if old_tree is None or new_tree is None:
        return False
    a, b = list(ast.walk(old_tree)), list(ast.walk(new_tree))
    if len(a) != len(b):
        return False
    parent = {c: p for p in b for c in ast.iter_child_nodes(p)}
    for x, y in zip(a, b):
        if type(x) is not type(y):
            return False
        if not isinstance(x, ast.Constant):
            if any(getattr(x, f, None) != getattr(y, f, None) for f in ("id", "attr", "arg", "name", "op")
                   if not isinstance(getattr(x, f, None), ast.AST)):
                return False
            continue
        if x.value == y.value and type(x.value) is type(y.value):
            continue
        if type(x.value) is not type(y.value) or bool(x.value) != bool(y.value):
            return False
        if isinstance(y.value, str) and not y.value.strip():
            return False
        n = y
        while n in parent and not isinstance(parent[n], (ast.ClassDef, ast.Module)):
            p = parent[n]
            if isinstance(p, ast.Lambda):
                break  # the body runs when called; coverage sees that
            if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return False  # the function rule owns it
            if isinstance(p, ast.UnaryOp) and isinstance(p.op, (ast.USub, ast.UAdd)):
                n = p; continue
            if isinstance(p, ast.Call):
                fname = p.func.attr if isinstance(p.func, ast.Attribute) else getattr(p.func, "id", "")
                if n is p.func or fname not in factories:
                    return False
                n = p; continue
            if not isinstance(p, _ALLOWED_UP):
                return False  # Subscript, BinOp, Compare, f-string...: evaluated at import
            n = p
    return True
```
In `Selector.__init__` (after line 216):
```python
        self.construct_only = {self.by_id[t] for t in self.index.get("construct_only_dialect_tests", []) if t in self.by_id}
        self._factories = set(self.index.get("closure_factories", []))
```
In `sqlglot_change`, after `changed = …` (line 487):
```python
        inert = bool(self.construct_only) and import_inert(old_tree, new_tree, self._factories)
```
And in the module branch (line 551):
```diff
                 hit = self.import_time_change(rel, ln, renamed)
+                if inert:
+                    # Construct-only tests (test_generator_memory) fail only by an import/construction
+                    # crash or a per-instance leak; a literal-only edit can do neither.
+                    hit -= self.construct_only - self.cov(rel, [ln])
```
Notes on this change:
- Leave the `module-level-unmapped` check as it is. The subtraction keeps the set non-empty in practice, but if `hit` does become empty, treat it as unmapped (run everything) rather than returning `[]`.
- `import_inert` compares whole files, so any other edit in the same file disables the gate.
- `self.cov(rel, [ln])` keeps gm when it really ran that line, as in the athena/bigquery `__init__`.

### C4. selector.py:32: extend exact-line pruning to tests ≥ 1 s (covers the cache test)
```diff
-SLOW_MS = 5000
+SLOW_MS = 1000  # exact-line pruning is sound for any test (unexecuted replaced line ⇒ unobservable)
```

### C5 (medium risk). Attribute-read gate for class-body edits in core classes (max_nodes)
For a top-level statement `NAME = …` in the class bodies of `Parser` (parser.py) or `Tokenizer` (tokens.py), use these tests in place of `file_cov(rel)`:
- the coverage of every line in sqlglot that reads `.NAME`. build_index adds `attr_reads[NAME] = [[rel, line], …]` from `ast.Attribute(attr=NAME, ctx=Load)`;
- plus the tests whose source names `.NAME` (scan the tests/ AST the same way);
- plus `class_tests`.

Fall back to today's `file_cov` when:
- NAME is read on any line inside an `import_fns` range (e.g. `KEYWORDS`, `SINGLE_TOKENS`, `QUOTES`, `SHOW_PARSERS` are read at import);
- or NAME appears as a string constant anywhere in sqlglot (dynamic `getattr`/settings);
- or the statement is not a plain single-target assignment.

```python
    def attr_read_tests(self, name: str) -> set[int] | None:
        reads = self.index.get("attr_reads", {}).get(name)
        if not reads or name in self.index.get("attr_dynamic", ()):
            return None
        if any(self.in_import_fn(r, ln) for r, ln in reads):
            return None
        out = set()
        for r, ln in reads:
            out |= self.cov(r, [ln])
        return out | {self.by_id[t] for t in self.index.get("attr_test_reads", {}).get(name, []) if t in self.by_id}
```
Call it from `_import_time_change` before the `file_cov` loop, only when `rel in ("sqlglot/parser.py", "sqlglot/tokens.py")`, and keep `file_cov` when it returns None. max_nodes then stays exactly when its run read the attribute.

## (d) Ranked recommendations

| # | change | sample s saved | hidden value | risk |
|---|---|---|---|---|
| 1 | C2: drop the `_build` prefix and seed callees stored into a module-level cache | 6.7 (fryt) | every edit inside one of ~120 parse builders stops pulling in import-time and all-dialect selections | **low**: sound, because those functions run per parse and are credited per test |
| 2 | C1 + C3: inert-literal gate for construct-only tests (gm) | 10.0 (535z 3.3, gbav 6.7), +1.6 on 4d63 after a tpcds gate | each literal tweak in a dialect class body (a common hidden shape) saves ≤ 6.7 | **low**: a miss needs a literal that raises at import through a display-only path |
| 3 | C4: SLOW_MS 5000 → 1000 | not measured (≤ 1–2) | trims the 1–2.5 s tests on window hits | **low**: same soundness argument as today |
| 4 | C5: attribute-read gate on Parser/Tokenizer class-body edits | 0 | ≥ 7 s (max_nodes) plus a large part of the near-full selection per edit | **medium**: dynamic reads; fallbacks listed above |
| 5 | Do not gate max_nodes or the cache test on function edits beyond exact coverage | 0 | — | Gating them would be **high** risk: crashes and timeouts on hot paths (round-1 R3) |
