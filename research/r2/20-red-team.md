# 20 — Red team: where `selector.py` can still miss on hidden commits

Method: read `selector.py`, `tools/build_index.py`, `tools/build_map.py`, and the pinned source; query
`maps/impact.json.gz` with read-only Python (json/gz plus `ast` on source text). I ran no selector, tools, tests or sqlglot.
`maps/index.json` isn't built locally, so I re-derived the parts of the index I needed (import-fn seeds, refs)
straight from the source with `ast`. The patch shapes copy the sample's mutation style: a flipped
operator, a changed constant, a deleted line, `False`→`True`.

## (a) Findings

### H1 (main hole): default values and decorators in a signature, in modules imported lazily. A miss is nearly certain.
- Defaults run once, when the `def` runs. For modules loaded during a test (dialect parsers and
  generators, `sqlglot/optimizer/*`, `executor/*`, `planner.py`, and factories in `dialects/dialect.py` and
  `transforms.py` called from generator class bodies), coverage gives the signature lines to whichever
  test imported the module first. Measured: `merge_subqueries.py:68-73` → 4 tests
  (test_canonicalize, test_qualify_semi_anti_join_column_aliases, ...). Lines 74-103 → 0.
- `selector.py:509-533` (the `fn` branch): window = ln±2, clipped to the function. When the signature is long
  or a docstring follows it, the window holds only header lines. `hit` comes back non-empty (the 4
  importers), so the fallbacks at `:521-531` never fire. That set is the whole selection.
- Measured across the map: of 1,075 default-value sites in functions with covered bodies, the
  window misses body-coverage tests at **162 sites** that are not import-fns.
  - 465 sites have an empty window and fall back to the whole function, which is OK.
  - 312 are OK and 136 are import-fns.
  - Spread: parsers/* 74, generators/* 33, dialects/dialect.py 10, optimizer 26, transforms 4,
    executor 6, planner 1.
- Verified examples (window → tests that run the body but go unselected):
  - `optimizer/merge_subqueries.py:70` `leave_tables_isolated: bool = False` → `True`. optimize() passes only
    the kwargs it was given (`optimizer.py` RULES loop), so test_optimize, test_merge_subqueries,
    test_tpch, test_tpcds and test_execute_tpcds all change. Selected: **4 tests. 93 missed (33 s).**
  - `optimizer/optimizer.py:60-66` (any default of `optimize`): 4 selected, 90 missed.
    Same for `pushdown_projections.py:17`, `pushdown_predicates.py:20`, `eliminate_ctes.py:13` and
    `canonicalize.py:12`.
  - `dialects/dialect.py:1445` `if_sql(name: str = "IF", ...)`. Window 1443-1447: line 1447 is the inner
    `def _if_sql`, run when each generator class body is built, so it is credited to 15 importers. The
    `return self.func(...)` lines 1448-1452 run at call time in **26 other tests**
    (test_snowflake, test_bigquery, test_duckdb...). All 26 are missed.
    Same pattern in `build_date_delta:1927`, `timestamptrunc_sql:1988`, `build_default_decimal_type:2789`
    and `transforms.preprocess:21`: 4 selected, 734 missed. Also `unnest_to_explode:311` and
    `explode_projection_to_unnest:415`.
  - `generators/bigquery.py:958` `cast_sql(..., safe_prefix=...)`: 4 selected, 44 missed.
    `dialects/snowflake.py:116` `can_quote`: 4 selected, 225 missed.
  - `executor/__init__.py:33-35` `execute(schema=None, dialect=None, tables=None)`: 1 selected, 50 missed.
    `executor/table.py:13`: 1 selected, 55 missed.
- `body_start()` (`:321`) has the same blind spot. It finds the first covered line, and the
  import-credited header counts as covered, so header lines also pass the `ln > body_start` guard
  for slow-test pruning.

### H2: own-body statements of factories that other modules call at import
`build_index.py` seeds `import_fns` only from calls made outside any def in the *same* file.
Factories such as `if_sql`, `build_date_delta*`, `build_formatted_time`, `timestamptrunc_sql`,
`transforms.preprocess` and `binary_from_function` are called from class bodies in *other* files.
Any statement in the factory itself (not in its closure) is credited to first importers only. Example:
`build_formatted_time` own lines → 59 tests, whole function → 72. Today almost every factory is just
`def _x(...)` plus `return _x`, so the chance of a hit is low. H1 covers the default values; H2 covers the rest.

### H3: deleting an enum member or class attribute that other code references by name
Removing a `TokenType` member that `parser.py` references (`TokenType.X` in a class-body table) breaks
`import sqlglot`, so every collected test fails. The selector takes `file_cov(tokenizer_core.py)` (1,565)
plus `class_tests(TokenType)`. That misses about 55 tests that never tokenize:
test_errors 13, test_expressions 8, test_dialect 5, test_time 4, test_helper 4, test_dialect_entry_points 3,
test_executor 3, and others.
`module_bound()` (`:721`) tracks only module-level names, so the `lost` → run-everything guard (`:556-566`)
never fires for class attributes.

### H4: plain builtins in the `executor/env.py` `ENV` dict
`"ARRAYAGG": list`, `"CURRENTDATE": datetime.date.today`, and so on. A lookup runs no line of env.py.
`import_time_change(env.py)` = `file_cov(env.py)` = 48 tests. `executor/python.py` is run by 53.
These 5 executor tests run executor code without any env.py line:
test_subquery_cardinality, test_array_slice, test_execute_catalog_db_table, test_case_sensitivity,
test_nested_table_reference.
test_execute_tpcds is safe because the whole executor/ directory is mapped to it.

### H5: module-level constants consumed at runtime by another module
Examples: `time.TIMEZONES` (used in `dialect.py:2006,2027`), `helper.DATE_UNITS`, `diff.UPDATABLE_EXPRESSION_TYPES`.
`import_time_change` takes `file_cov(<defining file>)` only. I checked all three: every user line's tests
already fall inside that file_cov (by luck: they also call `format_time` or `is_date_unit`), so today there are 0 misses. The general rule
is still a hole: a constant with no runtime function in its own file would miss.

### H6: residual gap in the tpcds proxy
The test_execute_tpcds child calls `optimize(..., leave_tables_isolated=True)` (`executor/__init__.py:76`).
The proxy tests use the default `False`. Lines reached only on the executor path are not mapped to tpcds.
I found no such line in merge_subqueries, where the flag is read on lines 453 and 190, both already mapped to tpcds.
Low risk, and only partly measurable.

## The 25 patch shapes, traced
OK = every plausible failing test is selected. Cost: ≈all means 1,500+ tests (k=8, roughly 100 s+).

| # | File / patch shape | Path in selector.py | Verdict |
|---|---|---|---|
| 1 | tokenizer_core `TokenType`: insert a member mid-enum (auto values shift) | module → file_cov + class_tests(TokenType) | OK, ≈all |
| 2 | tokenizer_core: delete `DCOLON = auto()` (still referenced) | module, not `lost` | **MISS** (~55 non-tokenizing tests), H3 |
| 3 | tokens.py `Tokenizer.KEYWORDS` value swap | module → file_cov(tokens) 1,567 + subclasses | OK, ≈all |
| 4 | dialect.py `class Dialect: NORMALIZE_FUNCTIONS` flag | module, family = every dialect | OK, ≈all |
| 5 | postgres.py `NULL_ORDERING` flag | family (redshift, materialize...) + name strings + all_dialect_tests | OK |
| 6 | duckdb flag read by the executor's `transpile(write="duckdb")` | name strings ("duckdb" in `cached_execute`) | OK, tpcds kept |
| 7 | dialect.py `if_sql(name="IF")` → `"IIF"` | fn window on the header | **MISS** 26 tests, H1 |
| 8 | expressions `arg_types` `False`→`True` on a Func | class refs + sql_name_tests | OK (like ktz3) |
| 9 | expressions: reorder `arg_types` keys (positional `from_arg_list`) | sql_name_tests via the `calls` index; `"*"` → all | OK |
| 10 | no-paren Func (`CurrentDate`) `arg_types` | refs: `currentdate_sql` lines run at call time | OK |
| 11 | parser.py `FUNCTIONS` lambda body | lambda rule | OK |
| 12 | parser.py `FUNCTIONS` value swap without a lambda | module → file_cov(parser) | OK, ≈all |
| 13 | dialect parser row `build_date_delta(exp.DateAdd, default_unit="DAY")`→`"MONTH"` | module → family + names | OK |
| 14 | dialect generator `TRANSFORMS` lambda edit / adjacent insert | lambda rule, plus import rule on its first line | OK |
| 15 | generator method default `cast_sql(safe_prefix=...)` (bigquery) | fn window on the header | **MISS** 44, H1 |
| 16 | optimizer rule body flip (simplify / eliminate_joins) | exact cov (threaded re-run) | OK (like 2jrq) |
| 17 | `merge_subqueries(leave_tables_isolated=False)`→`True`; `optimize()` defaults | fn window on the header | **MISS** 90+ tests incl. tpcds, H1 |
| 18 | optimizer `RULES` tuple reorder | module → file_cov(optimizer.py) | OK |
| 19 | `executor.execute(dialect=None)` default | fn window on the header | **MISS** 50, H1 |
| 20 | env.py `"ARRAYAGG": list`→`set` | module → file_cov(env) | small MISS risk, H4 |
| 21 | schema.py `_to_data_type` body flip | exact cov (129) | OK |
| 22 | time.py: delete a `TIMEZONES` entry | module → file_cov(time) 65 ⊇ user lines | OK (by luck), H5 |
| 23 | helper.py `seq_get` / `is_date_unit` flip | exact | OK, but expensive |
| 24 | serde / diff / lineage / jsonpath body flip; module constant | serde whole file → pool tests; others exact / file_cov | OK |
| 25 | `planner.Step.from_expression(... ctes=None)` default | fn window (1 test) | **MISS** 52 tests (tpcds kept: planner is mapped whole), H1 |
| + | `sqlglotc/setup.py` edit | → test_sqlglotc only; nothing else reads sqlglotc/ (grep of tests/) | OK |

## (b) Savings and recall risk
These are **recall** fixes. The cost on the sample should be ≈0 s: no sample commit edits a signature, a
cross-file factory, a class attribute deletion or an ENV entry. To confirm, count the new
`def-header`, `lost-class-attr` and `import-fn` reasons in `work/why.log` after one run (done by the coordinator,
not by me).

Expected misses on a hidden set of about 45 commits (about 24 code):
P(a constant mutation lands on a default value) × P(that site misses), where 162 of ~1,075 default sites miss.

| Hole | P(one or more hits in the hidden set) | × 300 s | Fix cost on hidden set |
|---|---|---|---|
| H1 defaults/decorators in lazily imported modules | ~0.06–0.10 | **18–30 s** | +the function's body tests only on a header edit |
| H3 deleted enum member/attr referenced as `Cls.attr` | ~0.01 | 3 s | run all on those rare commits |
| H2 cross-file factory own lines | ~0.01 | 3 s | ≈all on those rare commits |
| H4 ENV builtin entries | ~0.005 | 1.5 s | +5 tiny tests |
| H5 cross-module constants | ~0.005 | 1.5 s | small |
| H6 tpcds proxy on the executor-only path | ~0.005 | 1.5 s | none proposed |

## (c) Code changes

### C1 (H1): treat header lines as definition-time edits (`selector.py`)
Add a helper near `def_names`:
```python
def def_headers(tree) -> dict[int, tuple[int, int]]:
    """Old line -> (start, end) of the function whose header (decorators, signature) holds it."""
    out: dict[int, tuple[int, int]] = {}
    if tree is None:
        return out
    for n in ast.walk(tree):  # outer first, so nested defs overwrite
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = min([n.lineno] + [d.lineno for d in n.decorator_list])
            for ln in range(start, n.body[0].lineno):
                out[ln] = (start, n.end_lineno)
    return out
```
In `sqlglot_change`, compute `headers = def_headers(old_tree)` before the loop. Insert this as the **first**
branch, before `if fn and self.in_import_fn(...)`, so that a factory's default maps to its closure's
call-time coverage and not to "everything":
```python
            if fn and ln in headers:
                # Defaults and decorators run when the def runs, so coverage credits them to whichever
                # test imported the module. They matter to every call: take the whole function's
                # tests, including nested closures for factories.
                s, e = headers[ln]
                hit = self.cov(rel, range(s, e + 1))
                if not hit:
                    hit = self.import_time_change(rel, ln) or set(self.all)
                out |= hit
                reasons.append("def-header")
                continue
```
The `continue` also keeps header lines out of slow-test pruning, which fixes the `body_start` blind spot.

### C2 (H2): seed import_fns with factories that importing modules call at import (`build_index.py`)
After `internal_names` is built, before the `import_fns` loop:
```python
    top_calls: dict[str, set] = {}
    for rel, tree in trees.items():
        names, todo = set(), list(ast.iter_child_nodes(tree))
        while todo:
            n = todo.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(n, ast.Call):
                f = n.func
                nm = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                if nm:
                    names.add(nm)
            todo.extend(ast.iter_child_nodes(n))
        top_calls[rel] = names
    xseeds: dict[str, set] = {}
    for key, importers in internal_names.items():
        src, name = key.rsplit(":", 1)
        if any(name in top_calls.get(imp, ()) for imp in importers):
            xseeds.setdefault(src, set()).add(name)
```
Then, inside the per-file loop, after `scan(tree)`: `seeds |= xseeds.get(rel, set())`.
`in_import_fn` already leaves out nested def and lambda lines, so closure bodies keep exact coverage.
Only the factory's own lines become import-fns.

### C3 (H3): deleted class attribute referenced as `Cls.attr` elsewhere
In `build_index.py`, add `"qualified_attrs"`: `{"Cls.attr": [files]}` for every `ast.Attribute` whose
`.value` is a `Name` (or ends in an `Attribute`) named after a known class.
In `sqlglot_change`, next to the `lost` block:
```python
        def class_bound(tree):
            return {(c.name, t.id) for c in ast.walk(tree) if isinstance(c, ast.ClassDef)
                    for s in c.body if isinstance(s, (ast.Assign, ast.AnnAssign))
                    for t in (s.targets if isinstance(s, ast.Assign) else [s.target]) if isinstance(t, ast.Name)} \
                if tree is not None else set()
        qa = self.index.get("qualified_attrs", {})
        for cls, n in class_bound(old_tree) - class_bound(new_tree):
            if any(f != rel for f in qa.get(f"{cls}.{n}", [])):
                return set(self.all), "sqlglot:lost-class-attr"
```
Flipping a value keeps the binding, so this costs nothing on the sample. Subclass dialect flags read through
`self.dialect.X` don't match `Cls.attr`, so deleting one doesn't trigger run-everything.

### C4 (H4): executor module-level edits
In `_import_time_change`, before `return hit`:
```python
        if rel.startswith("sqlglot/executor/"):
            for f in ("sqlglot/executor/python.py", "sqlglot/generators/python.py"):
                hit |= self.file_cov(f)
```

### C5 (H5, optional)
In the index, store for each `internal_names` key the importer lines that use the name (the `refs`-style
tokenize pass). For module-level edits (`top_class is None`), add `cov` of the user lines for the
names the changed statement binds.

## Minimal guards that must survive cost-cutting
Each guard below is what keeps a sample commit or one of the shapes above at zero misses.
1. **Import-time rule for class bodies and module level** (`:547-555`, `_import_time_change`): file_cov of the dialect family,
   dialect-name strings, fixture `# dialect:` headers, `class_tests`, and `all_dialect_tests`.
   It carries 4d63, gbav, 535z and shapes 3-6, 12, 13, 18.
   Do not narrow it to the dialect's own test file: 535z's failure is in test_optimizer.
2. **`sql_name_tests`** for expressions/, including its `None` → all fallbacks (more than 200 subclasses, a `"*"` fixture).
   It carries ktz3 and shapes 8-10.
3. **Lambda rule plus the import rule on a lambda's first line or an adjacent insert** (`:535-546`). It carries 6ze8 and shapes 11 and 14.
4. **`import_fns` / `in_import_fn`** (fryt: closure factory in the same file). Widen it with C2; never remove it.
5. **Pool mapping in `build_map.py`**: serde whole file → pool tests (3utz), and executor/, planner and
   `generators/python.py` mapped whole to tpcds, plus the proxy lines. 2jrq and 3utz *really* fail tpcds.
   Any rule that drops tpcds must use this proxy coverage at line level and must never apply to these whole-mapped files.
6. **Slow-test pruning stays restricted** to same-indent replacements on a recorded line past the body start.
   Never extend it to insertions, multi-line statements or header lines. C1 makes header lines bypass it.
7. **Run-everything fallbacks**: `lost` internal names, package `__init__`, rename, binary,
   patch-did-not-apply, syntax error, fn-import-only, module-level-unmapped, and `fixture-unmapped`.
8. **`test_generator_memory` in `all_dialect_tests`.** It loads every dialect, so any dialect edit that could fail
   at import (H3-like deletions, bad table rows) fails it. Dropping it is safe only for edits that
   are provably value-only, and even then only if C3 is in place.
9. **`TYPING_TEST` on any `sqlglot/typing/` edit, and no docstring stripping in typing/.**
   **`sql_comment_only`** stays limited to `--` and empty lines.
10. **Never return `[]` because a change "looks semantically equivalent"** for 2qn5, 5y72 or 8y7j. Only AST- or
    docstring-identical edits with bytes-inert lines may return `[]`. Anything else trades 2 s for a 300 s risk.

## (d) Ranked recommendations
| Rank | Change | Saved s (sample) | Expected s saved (hidden, misses avoided) | Risk |
|---|---|---|---|---|
| 1 | C1: header lines → whole-function coverage, before the import-fn branch | 0 | 18–30 | very low (only adds tests) |
| 2 | Keep guards 1-10 against any cost cut; reject cuts that break one | 0 | protects 0-miss | — |
| 3 | C3: deleted `Cls.attr` referenced elsewhere → all | 0 | ~3 | low (fires rarely) |
| 4 | C2: cross-file factory seeds in import_fns | 0 | ~3 | low (≈all on those rare commits) |
| 5 | C4: executor module-level edits add python.py and generators/python.py file_cov | 0 | ~1.5 | none |
| 6 | C5: users of cross-module constants | 0 | ~1.5 | low |

Summary:
1. The main hole: default values and decorators in functions of modules imported lazily. The ±2 window sees only signature lines credited to the first importer, and selects 1–15 tests.
2. I verified 162 such sites in the map. Examples: `merge_subqueries(leave_tables_isolated=False)` (93 tests incl. tpcds missed), `optimize()` defaults, `if_sql(name="IF")`, `executor.execute(dialect=None)`, `bigquery.cast_sql`.
3. Fix C1 is about 15 lines in `selector.py`: send header lines to whole-function coverage before the import-fn branch. It costs ≈0 s on the sample and is worth roughly 18–30 s of expected misses on the hidden set.
4. Smaller holes, each worth 3 s or less: deleted enum members or class attributes (C3), cross-file factory bodies (C2), builtin `ENV` entries (C4), cross-module constants (C5). The other 18 shapes traced OK.
5. Ten guards must survive cost-cutting. The key ones are the import-time and family rules, `sql_name_tests`, the lambda and import-fn rules, the pool and tpcds mapping, narrow slow pruning, and `generator_memory` in `all_dialect_tests`.
