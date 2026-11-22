# 05 – Zero-fail commits, part B: fryt, gbav, ktz3, 2qn5, 5y72, 8y7j

All six are one-line "mutation-style" edits (`==`→`!=`, `<`→`<=`, `True`↔`False`, `copy=False`→`True`).
`sample/answers.json` gives `failing: []` for all six. Together they cost **33.4 s** (9.2 + 10.1 + 7.1 + 2.5 + 2.5 + 2.0).
I worked from the patch, the pinned source in `work/sqlglot/`, `work/why.log`, `output.jsonl`, `maps/impact.json.gz`
and `work/index.json`. Nothing was run.

## (a) Findings: why nothing fails, and why we pay for it

### 8y7j: the changed line never runs (dead in the suite). Cost 2.0 s, 1 test.
- `generators/clickhouse.py:55` `if scale == exp.UnixToTime.MICROS:` → `!=`.
- Map: lines 48, 49, 51, 53 and 54 are covered by `test_duckdb::test_time` only. **Lines 52 and 55-64 are absent from the
  line table.** The one test that reaches `_unix_to_time_sql` takes the MILLIS branch (54) and returns before 55.
- An unexecuted statement can't change behaviour, provided the edit doesn't change the function's compile-time shape
  (locals, `global`/`nonlocal`, `yield`/`await`, block structure).
- We still pay because `WINDOW=2` (`selector.py:30`, `:509-510`) takes lines 53-57, and so picks up `test_time` through line 53/54.

### fryt: an executed but weakly checked branch, plus a misclassified "import-fn". Cost 9.2 s, 37 tests.
- `parsers/dremio.py:39` `if len(args) == 2:` → `!=`, inside the closure `_builder` (38-55) returned by
  `build_date_delta_with_cast_interval` (33-57).
- Line 39 does run: map line 39 → `TestDialect::test_time` and `TestDremio::test_date_add`/`test_date_sub`. Line 55 (`fallback_builder`)
  has no coverage, so every test call has exactly 2 args. After the mutation, 2-arg calls go to `build_date_delta`
  (`dialects/dialect.py:1923-1940`), which builds `DateAdd(this, expression=CAST(.. AS INTERVAL HOUR), unit='DAY')`.
  `generators/dremio.py:15-27` prints a `DAY` unit as `DATE_ADD(a, b)`, so the SQL round trip in `test_dremio.py:190-204`
  is byte-identical. The test oracle can't tell the two ASTs apart. **No static rule can prove this, so the best
  achievable result is the ~3 tests that run line 39 (about 2.3 s).**
- **Bug: why.log reports `import-fn:37`.** `tools/build_index.py:232` has `once = once or name.startswith("_build")`, and
  `"_builder".startswith("_build")` is True. `defs` is keyed by bare name, so **every closure named `_builder` in every file
  is marked "runs at import"**. `work/index.json` lists dremio `[[38, 55, []], [33, 57, [[38, 55]]]]`, so the nested
  `_builder` is an import fn itself.
  - Index-wide: **125 import_fns entries are `_build*` defs (25 are closures named `_builder`), spread over 25 files.**
    Most of them are parse-time builders, called once per parse and covered accurately per test.
  - Effect: `in_import_fn` → `import_time_change` → coverage of the whole dremio family, `name_tests("dremio")`, and
    `all_dialect_tests` (this last one brings in **test_generator_memory, 6.686 s**).
  - The heuristic was meant for `_build_dispatch` (`generator.py:74`, cache-gated at `:1005-1008`),
    `_build_gen_dispatch` (`simplify.py:1988`) and `_build_coerces_to` (`annotate_types.py:157`). The last two are called at
    module level and are already seeds through `scan()`. Only `_build_dispatch` needs a structural "cache-gated" rule.

### gbav: an unreachable table entry (trie shadowing). Cost 10.1 s, 42 tests.
- `parsers/singlestore.py:213` `"GROUPS FOR USER": _show_parser(..., target=True)` → `target=False`.
- `Parser._find_parser` (`parser.py:10262-10284`) walks the trie and **returns at the first `TrieResult.EXISTS`**.
  `"GROUPS"` (`singlestore.py:211`) is a complete key, so `SHOW GROUPS FOR USER 'root'` (`test_singlestore.py:926`) dispatches
  to the `"GROUPS"` parser. `_parse_show_mysql` then reads `FOR USER 'root'` at `mysql.py:643`. The edited entry is dead code.
- An AST scan of every `SHOW_PARSERS`/`SET_PARSERS` dict literal finds 6 shadowed entries, all in singlestore:
  212, 213, 229, 230, 233 and 234 (`GROUPS|ROLES|USERS FOR ...`).
- The edit is also **import-safe**. `_show_parser` (`mysql.py:55-59`) is a pure closure factory (a nested `def` plus `return`), so
  a constant argument flip can't raise at import.
- Cost driver: a class-body edit → `import_time_change` → `all_dialect_tests` → test_generator_memory (6.7 s). The remaining
  41 tests sum to 1.41 s.
- test_generator_memory (`tests/test_generator.py:19-38`) imports every dialect and constructs generators 1,100 times. A parser
  edit can fail it **only** through an import or class-creation failure.

### ktz3: a required-flag flip that every construction site already satisfies. Cost 7.1 s, 134 tests, k=2.
- `expressions/ddl.py:82` `TriggerProperties.arg_types["constraint"]: False → True`.
- The only consumer of `arg_types` *values* is `required_args = {k for k, v in arg_types.items() if v}`
  (`expressions/core.py:110`), which `error_messages` reads (`:1322-1327`, it fails on `None` or `[]` only).
- The only construction site is `parser.py:2734-2747`, which passes `constraint=is_constraint`, a bool from the walrus at `:2704`.
  The value is never None, so validation can't fail. Tests only use `find(exp.TriggerProperties)` (`test_duckdb.py:378`).
- Why 134 tests: the module-level rule takes `file_cov("sqlglot/expressions/ddl.py")`, which is every test touching any DDL
  expression method. `class_tests("TriggerProperties")` is only about 5 tests (refs at `generator.py:1411`, `:1547-1574`,
  `generators/duckdb.py:2045` and `parser.py:2734`, all ≤0.33 s). `sql_name_tests` is empty (no `call_tests` for
  `triggerproperties`).

### 2qn5: an equivalent mutant (tuple-length boundary). Cost 2.5 s, 6 tests.
- `generators/clickhouse.py:811` `self.dialect.version < (23, 12)` → `<=`.
- `Dialect.__init__` (`dialects/dialect.py:1165-1167`) always makes `version` a **3-tuple** (padded with `"0"`; the default is
  `sys.maxsize`). A 3-tuple never equals the 2-tuple `(23, 12)`, so `<` and `<=` agree on every input. The mutant is semantically dead.
- Even ignoring that, tests only use `version=23.8` and `24.1` (`test_clickhouse.py:2302-2344`), and both evaluate the same way
  under either operator.
- Line 811 is executed by 6 tests, so coverage can't help. Only value reasoning gets to `[]`.

### 5y72: an equivalent mutant (copying a freshly built tree). Cost 2.5 s, 3 tests.
- `optimizer/eliminate_subqueries.py:176` `exp.alias_(exp.table_(name), ..., copy=False)` → `copy=True`.
- `alias_` (`expressions/core.py:3058-3112`) uses `copy` only in `maybe_parse(..., copy=copy)`, which copies when the input is
  an `Expr`.
- The input is `exp.table_(name)` (`builders.py:530-551`), a brand-new `Table`. `name` is a `str` from `_new_cte` (`:193-201`), so
  no shared child is re-parented either. Copying an unshared fresh tree only changes object identity, and `new_table` isn't used again.
- The sibling call at `:148` already uses the default `copy=True` on the same shape.
- Line 176 runs in 3 tests (map 168-179), so only value reasoning gets to `[]`.

## Principles (general, derived from source or coverage), with recall risk

| # | Principle | Sample hit | Hidden-set recall risk |
|---|-----------|-----------|------------------------|
| P1 | A same-indent one-line replacement of a statement start inside a function body, where **the line has no coverage entry at all** (not even import-credited), the function has other covered lines (so tracing worked there), the function's shape is unchanged (masked-AST equal) and its symbols are unchanged → `[]` | 8y7j | **Low.** A miss needs a coverage gap: the line runs only in a subprocess or pool, or only in a test the map lost. Mitigate by excluding files whose pool tests get proxy coverage (`sqlglot/optimizer/`, `executor/`, `planner.py`, `serde.py`; see `tools/build_map.py`). Patch shapes that miss: `x = 1` on a dead line making `x` local (blocked by the symbol check), `elif`→`if` (blocked by the masked-AST check), adding `yield` (blocked). |
| P2 | `import_fns` must not treat `_build*` names, or nested closures, as run-once. A nested def is import-time only if its enclosing import fn's own body calls it. Run-once comes from module-level calls, decorators, and **cache gating** (the call result is stored into a module-level dict) | fryt | **Very low.** It restores the coverage rule, which is accurate for parse-time builders. Only a `_build_x` that runs once per process *and* isn't cache-gated by the structural rule would lose its broadcast. I checked the three known ones: two are module-level and `_build_dispatch` is cache-gated. |
| P3 | An **import-safe** class-body edit (old and new differ only in `bool`/`int`/`str`/`None` constants, not in dict keys, and every call containing a changed constant targets a *pure closure factory*) in `sqlglot/parsers/` or `sqlglot/typing/` cannot break import, so it doesn't need **test_generator_memory**. Keep the cheap all-dialect tests. | gbav (−6.7 s), part of fryt | **Low-moderate.** A miss needs a constant that changes generator construction or memory. Generators aren't in scope (`generators/` and `dialects/` excluded). Shapes to watch: a parser constant read by a generator at init (none known), or a constant passed to a non-pure callee at import (excluded by the purity rule). |
| P4 | A **trie-shadowed entry**: in a dict consumed via `_find_parser` (`SHOW_PARSERS`, `SET_PARSERS`), an entry whose key has a word-prefix that is itself a key of the same final dict is unreachable. If the edit is confined to that entry's value, the key is unchanged and the edit is import-safe (P3), select `[]` | gbav (→0) | **Low.** It depends on `_find_parser`'s first-EXISTS return. Check that pattern at index build and disable the rule if it's absent. Misses: a subclass that pops the prefix key (none today), or a multi-file patch that also edits `_find_parser` (that file selects on its own merits). The rule fires rarely (6 entries). |
| P5 | An **`arg_types` value-only flip** (same keys, bool constants) in `sqlglot/expressions/` changes only `required_args`, so only construction-plus-validation can observe it. Select `class_tests(C ∪ subclasses ∪ classes that spread C.arg_types)` and drop `file_cov(expressions file)` and the all-dialect broadcast | ktz3 (7.1 → ~2.4) | **Low.** A miss needs a construction site outside the refs index (`getattr(exp, name)` from serde/`build`, or tests constructing `exp.C(...)` directly), so add `test_strings`/`sqlglot_names` hits for `C`. `True→False` relaxes validation and could only break a test expecting a "Required keyword" error at a construction site, which `class_tests` covers. |
| P6 | Equivalence by value domain: a relational boundary flip whose operand set reachable from tests evaluates identically (for example, a `dialect.version` compared to a literal, evaluated over all `version=` settings in test strings plus the default `maxsize`) | 2qn5 | **Moderate.** Sound only if the domain extraction is complete (Python `version=` kwargs, fixture headers). A generic `<`→`<=` rule without domain evaluation would be **high** risk (`len(args) < 2` sits on a common boundary). |
| P7 | A `copy=False → True` flip where the copied argument is a fresh constructor call with only `str`/constant args, and the result isn't aliased later | 5y72 | **Moderate-high in general.** `copy=True` on a builder whose receiver is mutated in place (`q.where(.., copy=False)` used for its side effect) changes behaviour completely. It is safe only when the flag reaches `maybe_parse(expr)` of a fresh object. That needs per-callee knowledge. |

## (b) Estimated savings on the sample (cost in s)

- P2: fryt 9.2 → about 2.3 (the window around 39 gives about 3 functional tests plus 4 import-credited ones on def line 38, ~0.4 s): **−6.9**.
- P3 + P4: gbav 10.1 → 0: **−10.1** (P3 alone: 10.1 → 3.4, −6.7).
- P5: ktz3 7.1 → ~2.4: **−4.7**.
- P1: 8y7j 2.0 → 0: **−2.0**.
- P6 and P7 (optional): 2qn5 −2.5 and 5y72 −2.5.
- **Total: ~23.7 s safe-ish (P1-P5), ~28.7 s with P6 and P7.** None of these touch a sample commit with a failing test. Commits whose
  why.log reason is `fn`/`module` in a failing area keep their current selection, because the rules only fire on narrow shapes.

## (c) Code changes

### C1 (P2): `tools/build_index.py` in the import_fns block (lines 226-258)
```diff
-                once = bool(deco_names & {"cache", "lru_cache", "cached_property", "cache_info"})
-                once = once or name.startswith("_build")
+                once = bool(deco_names & {"cache", "lru_cache", "cached_property", "cache_info"})
+                # (no name heuristic: "_build" matched every `_builder` closure and parse-time builder)
```
Add cache-gated seeds after `scan(tree)`:
```python
        top_names = {t.id for s in tree.body for t in
                     (s.targets if isinstance(s, ast.Assign) else [getattr(s, "target", None)])
                     if isinstance(t, ast.Name)}
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            var_of = {}  # local var -> callee name, from `v = callee(...)`
            for n in ast.walk(fn):
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and callee(n.value):
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            var_of[t.id] = callee(n.value)
            for n in ast.walk(fn):
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id in top_names:
                            v = n.value
                            if isinstance(v, ast.Call) and callee(v):
                                seeds.add(callee(v))           # CACHE[k] = build(...)
                            elif isinstance(v, ast.Name) and v.id in var_of:
                                seeds.add(var_of[v.id])        # x = build(...); CACHE[k] = x
```
Keep `defs` for top-level and class-level functions only, and keep nested defs inside a hot range only when the hot def calls them itself:
```diff
-        for node in ast.walk(tree):
-            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
-                defs.setdefault(node.name, []).append(node)
+        for scope in [tree] + [c for c in tree.body if isinstance(c, ast.ClassDef)]:
+            for node in scope.body:
+                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
+                    defs.setdefault(node.name, []).append(node)
 ...
-                nested = [[c.lineno, c.end_lineno] for c in ast.walk(d)
-                          if c is not d and isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]
+                called = own_calls(d)
+                nested = [[c.lineno, c.end_lineno] for c in ast.walk(d)
+                          if c is not d and (isinstance(c, ast.Lambda) or
+                          (isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)) and c.name not in called))]
```
(Nested classes such as a metaclass inside a class body are rare. If you're worried, keep `ast.walk` but skip defs whose parent is a FunctionDef.)
Check after rebuilding: dremio import_fns should be `[[33, 57, [[38, 55]]]]`, and `generator.py:_build_dispatch` should still be present.

### C2 (P1): `selector.py`, Selector method plus a hook in `sqlglot_change` `elif fn:` (before line 509)
```python
PROXY_PREFIXES = ("sqlglot/optimizer/", "sqlglot/executor/", "sqlglot/planner.py", "sqlglot/serde.py")

def unexecuted_stmt(self, rel, ln, fn, old_tree, new_tree, f) -> bool:
    table = self.files.get(rel, {}).get("lines", {})
    if str(ln) in table or rel.startswith(PROXY_PREFIXES):
        return False
    if not any(str(x) in table and self.test_set(table[str(x)]) for x in range(fn[0] + 1, fn[1] + 1)):
        return False                     # no evidence the function was traced in-process
    if any(len([l for l in h["lines"] if l[0] == "-"]) != len([l for l in h["lines"] if l[0] == "+"])
           for h in f["hunks"]):
        return False                     # line numbers must match old == new
    def stmt_fn(tree):
        best = None
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.lineno <= ln <= n.end_lineno:
                if best is None or n.lineno > best.lineno:
                    best = n
        st = next((n for n in ast.walk(best) if isinstance(n, ast.stmt) and n.lineno == ln), None) if best else None
        return best, st
    (of, os_), (nf, ns) = stmt_fn(old_tree), stmt_fn(new_tree)
    if not (os_ and ns and type(os_) is type(ns)):
        return False                     # also rejects continuation lines (no stmt starts there)
    def masked(fnode, st):
        saved = {k: getattr(st, k) for k in st._fields if k not in ("body", "orelse", "handlers", "finalbody")}
        for k in saved: setattr(st, k, None)
        try: return ast.dump(fnode)
        finally:
            for k, v in saved.items(): setattr(st, k, v)
    if masked(of, os_) != masked(nf, ns):
        return False                     # block structure changed
    def shape(st):
        hdr = [getattr(st, k) for k in st._fields if k not in ("body", "orelse", "handlers", "finalbody")]
        out = set()
        for h in hdr:
            for n in ast.walk(h) if isinstance(h, ast.AST) else [x for y in (h or []) if isinstance(y, ast.AST) for x in ast.walk(y)]:
                if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load): out.add(("bind", n.id))
                if isinstance(n, (ast.Yield, ast.YieldFrom, ast.Await, ast.Global, ast.Nonlocal)): out.add((type(n).__name__,))
                if isinstance(n, ast.Name) and n.id in ("super", "__class__"): out.add(("cell",))
        return out
    return shape(os_) == shape(ns)
```
Hook it in `sqlglot_change`:
```diff
             elif fn:
+                if ln in prunable and ln > self.body_start(rel, fn) and \
+                        self.unexecuted_stmt(rel, ln, fn, old_tree, new_tree, f):
+                    reasons.append("unexecuted")
+                    continue
                 lo, hi = max(fn[0], ln - WINDOW), min(fn[1], ln + WINDOW)
```
(Note: `same_indent_replacements` already blocks inserts, deletes and indent moves.)

### C3 (P3 and P4): index additions in `tools/build_index.py` and the selector hook
Index: `pure_factories` holds the names of top-level defs whose body (after the docstring) is only `FunctionDef`s and one `return Name|Lambda`.
`trie_shadowed` maps `rel` to `[[key_line, value_end_line]]` for `SHOW_PARSERS`/`SET_PARSERS` dict-literal entries whose key has a
word-prefix key in the same literal, or in any `**Base.X` spread resolved through class names across `sqlglot/parsers/`.
Emit `trie_shadowed` only if `parser.py:_find_parser` contains `if result == TrieResult.EXISTS:` followed directly by a `return`.
```python
def import_safe(old_stmt, new_stmt, pure: set[str]) -> bool:
    """Only constant leaves differ (no dict keys) and every Call around them is a pure factory."""
    if ast.dump(old_stmt, include_attributes=False).count("Constant(") != ast.dump(new_stmt).count("Constant("):
        return False
    def walk(a, b, calls):
        if type(a) is not type(b): return False
        if isinstance(a, ast.Constant):
            return a.value == b.value or (type(a.value) is type(b.value) and type(a.value) in (bool, int, str)
                                          and all(c in pure for c in calls))
        if isinstance(a, ast.Dict) and any(x is not None and y is not None and ast.dump(x) != ast.dump(y)
                                           for x, y in zip(a.keys, b.keys)):
            return False
        if isinstance(a, ast.Call):
            calls = calls + [getattr(a.func, "id", getattr(a.func, "attr", "?"))]
        fa, fb = list(ast.iter_fields(a)), list(ast.iter_fields(b))
        for (k, x), (_, y) in zip(fa, fb):
            xs, ys = (x, y) if isinstance(x, list) else ([x], [y])
            if len(xs) != len(ys): return False
            for p, q in zip(xs, ys):
                if isinstance(p, ast.AST):
                    if not walk(p, q, calls): return False
                elif p != q and k not in ("lineno", "col_offset", "end_lineno", "end_col_offset"):
                    return False
        return True
    return walk(old_stmt, new_stmt, [])
```
In the module-level branch of `sqlglot_change`, find the top-level statement spanning `ln` in old and new (`same_indent_replacements`
keeps lines aligned). If `import_safe(...)`:
- (P4) if `ln` lies in a `trie_shadowed[rel]` range and is not that range's key line → contribute nothing (`reasons.append("trie-dead")`; `continue`).
- (P3) if `rel.startswith(("sqlglot/parsers/", "sqlglot/typing/"))` → call `import_time_change(rel, ln, renamed, import_safe=True)`.
  In `_import_time_change`, drop `tests/test_generator.py::TestGenerator::test_generator_memory` from the `all_dialect_tests`
  loop when `import_safe` is set. Add the flag to the memo key.

### C4 (P5): `selector.py` module-level branch, `sqlglot/expressions/` only
When the old and new class statement differ only inside the `arg_types` dict value (same keys, every changed node is
`Constant(bool)`), compute:
```python
hit = set()
for c in {cls} | descendants(cls) | spread_users(cls):   # spread_users: classes whose arg_types literal has **C.arg_types
    hit |= self.class_tests(c)
hit |= {n for n, s in self.strings.items() if cls in s}   # tests naming the class
hit |= {x for m in self.index.get("sqlglot_names", {}).get(cls, []) for x in self.by_file.get(m, ())}
out |= hit; reasons.append("arg-flag"); continue          # skip import_time_change (file_cov of ddl.py)
```
`spread_users` can be computed from `refs[cls]` entries whose line sits in another class's `arg_types` assignment (index both).

## (d) Ranked recommendations

1. **C1 / P2: fix the `_build` prefix and nested-closure import_fns.** −6.9 s on the sample (fryt), and it removes test_generator_memory
   from every edit inside one of 125 builder functions on hidden sets. Risk: **very low**. It is a bug fix that makes the
   coverage rule apply where coverage is accurate.
2. **C3 / P3: no test_generator_memory for import-safe constant edits in parsers/typing.** −6.7 s (gbav), likely the biggest
   hidden-set win, because one-token table flips look common in this corpus. Risk: **low-moderate**. Keep generators/ and dialects/ out.
3. **C4 / P5: narrow `arg_types` flag flips to construction sites.** −4.7 s (ktz3). Risk: **low**.
4. **C2 / P1: `[]` for a replaced statement that coverage never saw.** −2.0 s (8y7j). Risk: **low**, with the proxy-file exclusion and
   the shape guards. It generalises to every "mutation on a dead branch" patch.
5. **C3 / P4: trie-shadowed entries → `[]`.** −3.4 s more on gbav (after P3). Risk: **low**, but it fires rarely. Implement it after 1-4.
6. P6, version-domain evaluation (−2.5 s, 2qn5): **moderate** risk and a niche pattern, so only if time allows.
7. P7, copy-flip on a fresh object (−2.5 s, 5y72): **moderate-high** risk in general, so **don't implement** without per-callee proof.

Nothing here maps patches to answers. Every rule is decided from the pinned source (AST shape, trie semantics, `arg_types`
consumers) or from coverage absence. The sample answers were used only to confirm that the six really fail nothing.
