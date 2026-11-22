# R2-03: the mid-cost optimizer tests (test_scope, optimizer test_tpcds, test_annotate_funcs, test_qualify_columns, test_agg_order, test_optimize_in_nulls)

No code was run. Everything below comes from reading `work/sqlglot/tests`, `maps/impact.json.gz`, `work/index.json`, `work/out.jsonl` and `sample/answers.json`.

## (a) Findings

### A1. What each test really depends on (`work/sqlglot/tests/test_optimizer.py` unless noted)
| test (number, s) | fixtures it reads | optimizer code it runs | what it compares |
|---|---|---|---|
| test_scope (1428, 2.5) | none | parse, `build_scope`/`traverse_scope`, `qualify` on small inputs; read="bigquery"/"duckdb"/"hive" (l.3388-3643). The cost is mostly parsing 10000-way and 1000-way UNION chains. | scope structure and `.sql()` strings |
| test_tpcds (1440, 2.2) | `optimizer/tpc-ds/tpc-ds.sql` only, via `check_file("tpc-ds/tpc-ds")` (l.3363) | full `optimize()` pipeline, `execute=False` | optimized SQL equals the fixture's expected text |
| test_annotate_funcs (1295, 2.0) | `optimizer/annotate_functions.sql` only (l.3956) | parse and `annotate_functions` for every dialect named in the fixture headers | `result.type.sql(dialect)` |
| test_qualify_columns (1382, 1.4) | `qualify_columns.sql` and `qualify_columns_ddl.sql` (check_file at about l.999 and l.1004), plus about 60 inline qualify calls | qualify, qualify_columns, scope, resolver; dialects trino, snowflake, bigquery, duckdb, postgres, spark, clickhouse... | qualified SQL text |
| test_agg_order (test_executor.py:1720, 1.4) | none. It only inherits `setUpClass`, which loads the tpc fixtures into attributes this test never uses. | `optimize()` and `Plan` on one 2-column AVG query | only the order of `plan.root.aggregations` aliases |
| test_optimize_in_nulls (2456, 1.3) | none | `optimize(..., dialect="duckdb")` on about 180 generated IN/ANY queries | duckdb result rows of the original query equal those of the optimized query (semantic check) |

Line coverage agrees with this. The only dialect-specific files these tests run are:
- test_scope: bigquery, duckdb and hive parsers.
- tpcds, agg_order, innulls: `dialects/dialect.py` and `typing/__init__.py` only (innulls also runs the duckdb files).
- annotate_funcs: all 14 dialect families.
- qualify_columns: about 12 families, including snowflake, clickhouse and redshift.

### A2. Selection waste in the sample (work/out.jsonl vs answers.json)
Selected vs selected-and-failing, over 30 commits:

| test | selected | failed when selected | wasted s |
|---|---|---|---|
| scope | 3 | 0 | 7.5 |
| tpcds (optimizer) | 4 | 2 (2jrq, 3utz) | 4.4 |
| annotate_funcs | 5 | 2 (j6uz, rszg) | 5.9 |
| qualify_columns | 7 | 3 (3utz, ary6, rszg) | 5.6 |
| agg_order | 1 | 0 | 1.4 |
| in_nulls | 4 | 0 | 5.3 |
| qualify_columns_using_after_outer_join | 3 | 0 | 0.6 |

That is about 30 s of selected-but-useless test time. It is much smaller in **cost**. Re-splitting with the selector's LPT gives at most **4.5 s** (3utz 1.9, bj7g 1.4, ktz3 0.7, 535z 0.6). The reason is that the 21.5 s `test_execute_tpcds` or `generator_memory` sets the max worker load on most of those commits. So gating these six tests directly is worth about 4.5 s on the sample, even with perfect pruning.

### A3. The big find: substring fixture matching selects the whole optimizer and executor suites (selector.py:271-278, 338-345, 633-636)
`fixture_tests()` and `fixture_change()` test `stem in s` (substring) over every string in the test. The strings come from `tools/build_map.py:test_strings`, which adds the consts of **every non-test helper in the class and of every module-level non-test statement** to every test. In `TestOptimizer` these include:
- `"optimizer/"`, the prefix of `check_file`'s f-string `f"optimizer/{file}.sql"` (l.226);
- `"validate_qualify_columns"`, from `meta.get(...)` in `check_file`.

Verified against `test_strings` in the map: all 167 `test_optimizer.py` tests contain `"optimizer/"`. `test_executor.py` contains `"/optimizer/tpc-ds/"` from the module-level `DIR_TPCDS` (l.30-31) and `"optimizer/tpc-ds/tpc-ds.sql"` from `setUpClass`, so all 60 executor tests contain them. So these stems match every test of those classes:
- `optimizer` (from `optimizer.sql`): 231 tests, 51.5 s.
- `canonicalize` and `qualify_columns`: 167 tests, 19.8 s each (via the helper string `validate_qualify_columns`).

Substring versus whole-basename matching, computed over the map's strings:

| fixture | substring | basename match |
|---|---|---|
| optimizer.sql | 231 tests, 51.5 s | 3 tests, 0.5 s (`test_optimize` is the only real reader) |
| canonicalize.sql | 167 tests, 19.8 s | 1 test (`test_canonicalize`, 1.0 s) |
| qualify_columns.sql | 167 tests, 19.8 s | 1 test (1.4 s) |
| tpc-ds.sql / tpc-h.sql | 61 tests | 61 tests (see A4) |

**Why this hits code commits too.** `import_time_change` -> `name_tests(words)` pulls in `fixture_dialects[word]`, which lists every fixture with a `# dialect: <word>` header, then calls `fixture_tests` on each. For snowflake that list includes `optimizer.sql`, `qualify_columns.sql` and others. Any import-time or class-body edit in a dialect that appears in those headers therefore selects the whole optimizer and executor suites.

**Sample evidence, commit 4d63** (snowflake Tokenizer class constant, `NUMERIC_ESCAPES`; fails 0 tests; 561 selected, cost 27.5 s):
- 167 `test_optimizer.py` tests and 60 `test_executor.py` tests are selected.
- 197 of those 227 are neither in the coverage of the 4 snowflake family files nor name "snowflake" in their strings. They total 46 s and include `test_execute_tpcds` (21.5 s), `test_execute_tpch`, `test_optimized_tpch`, `test_scope`, `test_tpcds`, `test_agg_order` and `test_optimize_in_nulls`.
- Re-splitting without those 197 gives **27.5 s -> 12.7 s, saving 14.8 s**. What remains is `generator_memory` (6.7 s, the `all_dialect_tests` rule) plus real snowflake readers.
- This is also why all of scope, tpcds, agg_order, innulls and qc_using were selected for 4d63. Their own coverage never touches a snowflake file.

On the hidden set, expect the same for any dialect edit at class-body, import or module level where the dialect has fixture headers. In the sample that is 4d63, 535z, gbav, ktz3 and fryt as `module` or `import-fn`, but only snowflake's family reached the fixtures.

### A4. Executor class helpers
`TestExecutor.setUpClass` (test_executor.py:100-119) loads the tpc csv files and `.sql` fixtures for every test. This is why an edit to `tpc-ds.sql` selects all 60 executor tests. Only `test_execute_tpch`, `test_execute_tpcds`, `test_optimized_tpch` and `_mp_execute` use `self.tpch_sqls` / `self.tpcds_sqls` (l.158, 167, 188, 191). However, a `setUpClass` failure (for example a malformed csv) fails every test in the class, so I would leave the csv and tpc `.sql` rule as is. See rec R4.

### A5. Why exact-line pruning for mid tests is unsafe (2jrq)
The existing exact-line rule applies only to tests of at least 5 s (`SLOW_MS`). Extending it to the mid tests looks attractive, because in 2jrq (eliminate_joins.py:71) the changed line is covered by tpcds alone, while the statement-first line (l.64-65) pulls in innulls and qc_using.

But the mutation there is `and` -> `or` as the **first token of a continuation line**. A test that evaluated the previous operand (l.70) as falsy and short-circuited never executed line 71. After the edit, line 71 is evaluated and the result changes. Exact-line coverage would therefore miss a test that really can be affected.

The same applies to any operator at the start of a continuation line, or to a ternary or boolean chain across lines. So exact-line pruning is only sound for **single-physical-line simple statements** (the whole statement on one line). See R3.

### A6. Observation-side gates were considered and rejected
Gating on "assertion only reads X" would need dataflow on test bodies:
- test_agg_order observes only aliases.
- test_scope observes only scope structure.
- test_optimize_in_nulls observes only duckdb semantics.

All the coverage still says the lines run, and I cannot show without running tests that a generator or parser edit on those lines cannot change what they observe. For example, `generator.py` lines used by `.sql()` in test_scope's string comparisons are observed. I recommend no such gate.

## (b) Estimated savings and recall risk

| change | sample saving | hidden-set estimate |
|---|---|---|
| R1 basename fixture match (selector) | **14.8 s** (4d63 only) | 10-25 s: class-body edits in snowflake, bigquery, duckdb, postgres and similar (about 3 of 45), plus fixture edits to optimizer.sql, canonicalize.sql or qualify_columns.sql (each 8-15 s) |
| R2 helper-aware `test_strings` | 0 | small; mostly makes R1 stricter and more robust |
| R3 exact-line pruning for mid tests, single-line statements only | about 1.4 s (bj7g: qualify_columns dropped, l.791 is a one-line assignment) | about 2 s |
| R4 limit tpc fixture rule to its readers | 0 | 0-8 s, risky |

Recall risks:
- **R1.**
  - A reader that builds the fixture name at runtime, for example `"tpc-" + x`, or one that globs or lists a directory, would no longer match. I grepped `tests/` and found only literal names: `check_file("x")`, `load_sql_fixture(_pair)s("optimizer/x.sql")`, `load_sql_fixture_pairs("identity.sql")` and `FIXTURES_DIR` joins for jsonpath.
  - The fixed matcher is applied to `.sql` only. Non-sql fixtures (csv, json) keep substring matching.
  - If there is no exact hit, the existing parent-dir fallback still runs. It would select the same large sets, which is safe but slow.
  - The shape that would cause a miss is a future test that builds the filename from pieces. That cannot occur in the pinned tests.
- **R3.** An edit in a one-line statement that coverage records but whose effect is read by a different line the test also ran is still caught, because that line is hit by the same test. The risk is mainly the continuation-line case (A5), which is excluded.

## (c) Code changes

### C1. `selector.py`: whole-basename fixture matching
Add a module-level helper near `HUNK`:
```python
def fixture_named(stem: str, s: str) -> bool:
    """s names the fixture file `stem`: its last path component, minus ".sql", is the stem.
    Substring matching would match "optimizer/" (a helper's f-string prefix) to optimizer.sql."""
    b = s.rsplit("/", 1)[-1]
    if b.endswith(".sql"):
        b = b[:-4]
    return b == stem
```
`Selector.fixture_tests` (about l.338-345), which is used by `name_tests` for dialect fixtures:
```diff
-            memo[stem] = {n for n, strings in self.strings.items() if any(stem in s for s in strings)}
+            memo[stem] = {n for n, strings in self.strings.items() if any(fixture_named(stem, s) for s in strings)}
+            # fixture_dialects only lists .sql fixtures, so no substring fallback is needed here.
```
`Selector.fixture_change` (about l.633-636):
```diff
         stem, parent = path.stem, path.parent.name
-        hits = {n for n, strings in self.strings.items() if any(stem in s for s in strings)}
+        if path.suffix == ".sql":
+            hits = {n for n, strings in self.strings.items() if any(fixture_named(stem, s) for s in strings)}
+        else:
+            hits = {n for n, strings in self.strings.items() if any(stem in s for s in strings)}
         if hits:
             return hits, "fixture-stem"
```
Keep the `fixture-dir` and `fixture-unmapped` fallbacks as they are. Note that `fixture-dir` uses the parent name as a substring, so it behaves as before when a fixture has no exact reader.

This keeps every real reader: `check_file("qualify_columns")` gives the string `qualify_columns`, `load_sql_fixture_pairs("optimizer/annotate_functions.sql")` gives basename `annotate_functions.sql`, and `check_file("tpc-ds/tpc-ds")` gives basename `tpc-ds`.

### C2. `tools/build_map.py` `test_strings` (optional hardening, needs a map rebuild, so I would do it only if a rebuild is planned)
Count a helper's or module-level function's strings only when the test references it, directly or transitively:
- Collect `Name` and `Attribute` identifiers used in the test body (`self.check_file`, `qualify_columns`, ...).
- Add the consts of the class helper methods and module-level functions or classes with those names, recursively.
- Add module-level constants that the test names (for example `TPCH_SCHEMA`, `DIR_TPCH`).
- Add the consts of `setUp` / `setUpClass` only if the test references an attribute those methods assign (`self.x` / `cls.x`).
- Keep the full old union for helpers in `tests/helpers.py`-style files that are not classes.

Do not apply the attribute rule to the executor `setUpClass` csv and tpc fixtures. A `setUpClass` failure fails every test, so either keep the old behaviour for `setUpClass` or leave it out of this change.

### C3. `selector.py` (mid-cost exact rule, low priority)
In `sqlglot_change`, replace `hit & self.slow` with `hit & self.exact_set`, where `self.exact_set = slow | {mid tests >= 1.0 s}`. Gate it with:
```python
single = ln in single_line_stmts(new_tree)   # ast stmt with lineno == end_lineno, not in a multi-line expr
if hit & self.exact_set and ln in prunable and single and ln > self.body_start(rel, fn): ...
```
`single_line_stmts` collects `{n.lineno for n in ast.walk(tree) if isinstance(n, ast.stmt) and n.lineno == n.end_lineno}`, excluding compound statements whose header spans lines. Continuation lines, as in 2jrq, fail this test and keep the full window.

## (d) Ranked recommendations
| # | recommendation | expected s saved | risk |
|---|---|---|---|
| R1 | C1 basename fixture matching | 14.8 s on the sample (4d63), plus likely 10-25 s on hidden | low. Any reader with a literal fixture name still matches, and a miss falls back to the dir rule. |
| R2 | C2 helper-aware test strings, only if a map rebuild is planned | 0 on the sample; extra protection against similar false matches | low-medium. It could drop a legitimate helper dependency if a reference is missed, so keep the union for anything unresolved. |
| R3 | C3 exact-line pruning for mid tests, single-line statements only | about 1.4 s | medium. It is unsafe for continuation lines (2jrq shape); never apply it to class-body, import or default-arg lines. |
| R4 | limit tpc fixture edits to their readers (4 executor tests) | 0 on the sample | medium-high. `setUpClass` failures fail the whole class, so I do not recommend it. |
| R5 | assertion-side gates (agg_order, scope, innulls) | 0-1.4 s | high. No sound static gate exists, so skip. |

Not recommended: directly gating scope, tpcds, annotate_funcs, qualify_columns, agg_order and innulls. Their coverage-derived selection is mostly sound. The cost of their wrong selections on the sample is at most 4.5 s, because bigger tests set the max worker load. The real waste is the false fixture-name match (R1).
