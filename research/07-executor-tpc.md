# 07 - What test_execute_tpch / test_execute_tpcds really run, and a tighter proxy

## (a) Findings

1. **test_execute_tpch executes zero queries.** `mp_execute` returns `None` unless `meta.get("execute")` is truthy (`work/sqlglot/tests/test_executor.py:57-59`). `tests/fixtures/optimizer/tpc-h/tpc-h.sql` contains no `# execute:` header (`grep -c execute` = 0, `grep -n '^#'` = nothing). So the 3.9 s test only does `setUpClass` (duckdb load, CSV parse), spawns a pool, `parse_one`s 22 queries, pickles them through `serde` and gets `None` back (`:175-185`). No optimizer, planner, executor or Python generator code runs for it. The map currently gives it all of that anyway.
2. **test_execute_tpcds executes 57 of 99 queries** (`grep -c '^# execute: true'` on `tpc-ds.sql` = 57). Per query, in the child process, `execute(expr, schema=TPCDS_SCHEMA, tables=...)` runs this (`sqlglot/executor/__init__.py`):
   - `ensure_tables`, `ensure_schema` (`schema.py`, `executor/table.py`).
   - `optimize(sql, schema, leave_tables_isolated=True, dialect=None)` (`__init__.py:~70`). That is the full default `RULES` tuple (`optimizer/optimizer.py:36-51`): qualify (which pulls in qualify_tables, qualify_columns, normalize_identifiers, isolate_table_selects, resolver, scope), pushdown_projections, normalize, unnest_subqueries, pushdown_predicates, optimize_joins, eliminate_subqueries, merge_subqueries, eliminate_joins, eliminate_ctes, quote_identifiers, annotate_types (plus `sqlglot/typing/*`), canonicalize, simplify. The extra kwargs are `leave_tables_isolated=True`, `isolate_tables=True`, `quote_identifiers=False`.
   - `Plan(expression)` (`planner.py`).
   - `PythonExecutor(tables).execute(plan)` (`executor/python.py`, `context.py`, `env.py`, `table.py`).
   - Python-dialect generation: `executor/python.py:11` imports `PythonGenerator` from **`sqlglot/generators/python.py`**, and `Python(Dialect)` is at `executor/python.py:553-557`. It also runs `generator.py` and `dialects/dialect.py` through the subclass.
   - `parse_one`, `find_tables`, `transpile(..., write="duckdb")` (`:181`, `:60`, `:129`) and `subtestHelper` run in the **parent** process. Coverage sees them natively, so no proxy is needed for the parser, tokenizer or duckdb generator.
3. **The current proxy** (`tools/build_map.py:134-156`) tags `test_execute_tpch` and `test_execute_tpcds` onto:
   - every line covered by `TestExecutor::test_optimized_tpch`, `TestOptimizer::test_tpch` or `TestOptimizer::test_tpcds`;
   - every line of `sqlglot/executor/*` and `sqlglot/planner.py` ("whole").

   Map facts (read from `maps/impact.json.gz`):
   - 8753 lines are tagged with an exec test.
   - 6699 lines are covered by optimizer `test_tpcds`.
   - Only **278 lines** are covered by the tpch proxies and NOT by `test_tpcds`: 61 optimizer, 49 generators, 18 dialects, 10 expressions, 139 top-level. So the tpch proxies add little breadth.
   - `test_optimized_tpch` never calls `optimize`. It only runs duckdb SQL (`:158-163`). It adds nothing real and is already covered natively in the parent process.
4. **`sqlglot/generators/python.py` has 0 lines tagged to either exec test** (map: 95 lines, 0 exec-tagged). "Whole" lists `sqlglot/executor/` and `planner.py` but not `generators/python.py`. The real path imports and uses it. Its other users are the in-process test_executor tests, 53 tests in all, e.g. `test_py_dialect`.
5. **The proxy is not a strict superset of the real path.**
   - Real execution uses `leave_tables_isolated=True`. The tpcds optimizer fixture runs with the default `False` (`check_file`, `tests/test_optimizer.py:~219-235`, and the `test_tpcds` call at `:3363-3366`). The flag changes `merge_subqueries` decisions (`merge_subqueries.py:453`: `leave_tables_isolated and len(outer_scope.selected_sources) > 1`). So the plans that come out differ. Later rules (pushdown_*, eliminate_subqueries, simplify, optimize_joins, planner) then see different shapes and may take branches the default-flag tpcds run never touches. Per-line coverage cannot see this.
   - The executed subset is 57 queries, and it is a subset of the 99 optimized in `test_tpcds`. That direction is fine. The isolated-tables shapes are not covered.
   - **Net: the proxy over-selects on some lines and under-selects on others.** The missing parts are `generators/python.py` and the lines reached only with isolation.
6. **Exact coverage is already scripted and has not been run.** `tools/coverage_executor.sh` swaps `ProcessPoolExecutor` for `ThreadPoolExecutor` and writes `.coverage.exec`. `build_map.py:79-86` merges it if present. `work/sqlglot/` has only `.coverage` and `.coverage.opt` (Nov 20 18:47 and 18:13). No `.coverage.exec` exists, so the 18:48 map is proxy-only. Even once it exists, `build_map.py:141-156` would still add the proxy on top, which keeps the over-breadth.

## (b) Recall risks and cost waste

Recall:
- A patch confined to `generators/python.py` (e.g. a Python-dialect `*_sql` method or transform) selects the in-process executor tests, but not `test_execute_tpcds`. If a tpcds query alone exercises it (a rare function or a date op), that is a miss, at 300 s each.
- A patch in a rule branch reached only under `leave_tables_isolated=True` plus isolated-table shapes (merge_subqueries/pushdown/simplify) is under-tagged. Other tests, such as the many in-process `execute()` tests in test_executor, may still catch it. This is lower risk, but real.

Waste:
- `test_execute_tpch` is tagged onto every optimizer, planner and executor edit, although it never touches them. It costs 3.9 s of load and keeps a worker busy.
- `test_execute_tpcds` (21.5 s) is tagged on lines that only the 42 non-executed queries or only the tpch queries reach. The tpch-only part is at most 278 lines and is small. The 42 non-executed queries are the bigger part and cannot be sized without the exact run.
- Most of the 6.7k lines are shared by the real path, e.g. simplify, qualify_columns and scope, so the exact map will shrink selection less than hoped. It is still the principled version, and I can't quantify the gain without running it.

## (c) Proposed changes

Primary (data only, no code in `selector.py`): run `tools/coverage_executor.sh`, which is a user-side step because I may not run it. Then change `build_map.py` so the proxy is a fallback, not an addition. Gate it on actual contexts, because thread-run coverage contexts are worth a sanity check (see the assertion below).

Diff against `tools/build_map.py`, replacing lines ~134-156 (from the `# test_execute_tpch/tpcds run execute()` comment through the end of the `for rel, entry in files.items()` loop):

```python
    # TPC executor tests.  tpch has no `# execute: true` queries, so it never reaches the
    # optimizer/planner/executor; only tpcds does.
    tpcds = index.get("tests/test_executor.py::TestExecutor::test_execute_tpcds")
    exact = (ROOT / ".coverage.exec").exists()
    if exact and tpcds is not None:
        # .coverage.exec (ThreadPool re-run) already holds the real lines. Sanity check it.
        simp = files.get("sqlglot/optimizer/simplify.py", {"lines": {}})["lines"]
        got = sum(1 for s in simp.values() if tpcds in set_table[s])
        assert got > 100, f"coverage_executor.sh produced no tpcds context ({got} simplify lines)"
    elif tpcds is not None:
        # Fallback proxy: ONLY optimizer test_tpcds (not test_tpch, not test_optimized_tpch),
        # and tag ONLY test_execute_tpcds.
        proxy_ids = {index["tests/test_optimizer.py::TestOptimizer::test_tpcds"]}
        for rel, entry in files.items():
            lines = entry["lines"]
            whole = (rel.startswith("sqlglot/executor/")
                     or rel in ("sqlglot/planner.py", "sqlglot/generators/python.py"))
            if whole:
                n = (ROOT / rel).read_text().count("\n") + 2
                for ln in range(1, n):
                    lines.setdefault(str(ln), intern(set()))
            for ln, sid in list(lines.items()):
                members = set(set_table[sid])
                if whole or members & proxy_ids:
                    lines[ln] = intern(members | {tpcds})
```

Also keep tpch tied to the pickle lines. This is already done through `pool_tests` at `build_map.py:119-120`, which includes both exec tests on `serde.py` and `__reduce__`. Nothing else is needed for tpch. A change to the tpch fixture or to `tests/test_executor.py` is already handled by file-level test mapping in `build_index.py`.

For the `leave_tables_isolated` under-coverage in the fallback case, optionally also tag `test_execute_tpcds` on whole functions of the isolation-sensitive modules (a cheap function-level widening):

```python
    # fallback only: merge_subqueries is where leave_tables_isolated changes plan shape
    for rel in ("sqlglot/optimizer/merge_subqueries.py", "sqlglot/optimizer/isolate_table_selects.py"):
        for ln in range(1, (ROOT / rel).read_text().count("\n") + 2):
            lines = files[rel]["lines"]; sid = lines.get(str(ln))
            lines[str(ln)] = intern((set(set_table[sid]) if sid is not None else set()) | {tpcds})
```

Note: with `.coverage.exec` present, `isolate_tables` and `merge_subqueries` run with the real flag, so this is unnecessary.

`selector.py`: no change, assuming it treats the exec tests as ordinary mapped tests.

## (d) Ranked recommendations

1. **Run `tools/coverage_executor.sh`, rebuild the map, and use the "exact" branch above.** This gives true per-line coverage of the 57 executed queries with `leave_tables_isolated=True` and the real Python generator. Impact: closes the `generators/python.py` and isolated-tables recall gaps, and removes the vacuous tpch tagging. It could also trim some of the tpcds waste. Check the assertion and `merged .coverage.exec` in the build log. If threads lose the per-test context (check `pytest_exec.log` and the assertion), keep the fallback.
2. **Add `sqlglot/generators/python.py` to the "whole" set now, in the fallback.** It is the only clear recall hole and costs a few commits at most. It is cheap and safe.
3. **Stop tagging `test_execute_tpch` with optimizer/planner/executor lines.** It runs none of them. This saves 3.9 s on every optimizer or executor commit and cannot reduce recall: it still gets `serde` and `__reduce__` lines through `pool_tests`.
4. **Drop `test_optimized_tpch` and `test_tpch` from the proxy list.** They add 278 lines at most and do not run `execute()`. Low impact, but it is hygiene.
5. Do not widen the proxy to all of `test_optimizer`. The real path uses the default `RULES` over `Scope` and the optimizer files already covered by `test_tpcds`. Per-line `test_tpcds` coverage plus the exact re-run is both narrower and closer to the real thing.
