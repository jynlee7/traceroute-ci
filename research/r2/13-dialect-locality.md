# 13 - Dialect locality: method edits vs class-body edits

Read-only analysis of `maps/impact.json.gz`, `work/index.json`, `selector.py`, `tools/build_index.py`. Nothing was run. The numbers come from inspecting the map with small Python snippets that re-derive the selector's family and name rules. The model reproduces the host's 561-test selection for 4d63 exactly.

## (a) Findings

**F1. Method-level edits in dialect files already use line coverage only.**
- `selector.py:sqlglot_change` takes the `fn` branch. That branch is `cov(rel, window +-2)`, with the slow-test exact-line rule on top.
- Family plus name widening (`dialect_family`, `name_tests`, `all_dialect_tests`, `fixture_dialects`) runs only from `import_time_change` (selector.py:385-412). It is reached for class-body or module-level edits, `in_import_fn` lines, and uncovered fallbacks.
- Uncovered-function fallbacks are negligible. Of 1436 functions in dialect dirs, 1 has no recorded lines (generators/duckdb.py:781-791). 5 have only empty test sets, all in dialect.py.
- So the proposal "method edit goes to line coverage only" is already the code path. The real leak is that many method edits are misclassified as import-time (F2).

**F2. `build_index.py` marks parse-time builders as import-time.**
- Line ~211: `once = once or name.startswith("_build")`. It catches every `_build_*` helper and every `_builder` closure.
- 118 of the 148 `import_fns` entries in dialect dirs are `_build*` or `_builder` functions that only run at parse or generate time.
- Examples: parsers/dremio.py:38-55 (`_builder`), parsers/snowflake.py (21 entries), parsers/tsql.py (13), parsers/bigquery.py (18), generators/duckdb.py:695,948,1249, and dialects/dialect.py 8 entries (`_builder` at 1897, 1929, 1949, 2636...).
- The only intended target was `generator.py:74 _build_dispatch`, which stays covered by the name-less cache evidence or an allowlist.
- Every such edit goes `in_import_fn` -> `import_time_change` -> whole family coverage + name tests + `all_dialect_tests`, which includes generator_memory at 6.7 s.
- Measured over the 118 entries, a mean selection costs:

| Selection | Tests | Seconds |
|---|---|---|
| Today (wide) | ~371 | 27.0 |
| Function coverage | ~9.6 | 2.7 |
| Line window +-2 | ~5.9 | 1.7 |

- Per file, wide vs line window: duckdb 594 tests / 66 s vs 6 / 1.2 s; snowflake parsers 360 / 17.7 s vs 4 / 2.3 s; dremio 37 / 7.2 s vs 3 / 0.1 s.
- Sample witness: fryt (`parsers/dremio.py:39`, `_builder`). It selected 37 tests at cost 9.2 and failed nothing. Line coverage gives about 3 tests.

**F3. Subclass dialects are already handled by coverage.**
- Line coverage is per executing line, whatever the calling dialect. Parent lines show tests that ran through subclasses:
  - generators/hive.py: 135 of 436 recorded lines are hit by test_spark or test_databricks.
  - parsers/hive.py: 104 of 219.
  - generators/spark2.py: 62 of 208.
  - parsers/spark.py: 41 of 97.
  - generators/mysql.py: 87 of 441 hit by doris, starrocks and singlestore tests.
  - generators/postgres.py: 90 of 455 hit by redshift, risingwave and materialize tests.
  - generators/presto.py: 269 of 461 hit by trino and athena tests.
- An override in the subclass makes the parent's line simply not run for that dialect, which coverage reports correctly.
- A newly added method override is covered by the `new-def` rule (`new_method_tests`).
- Conclusion: no family or name widening is needed for method edits. The family walk only matters for class-body edits.

**F4. Widening on top of coverage for class-body or table edits.**
Per dialect, tests in `file_cov(family)`, tests added by `name_tests`, and the union:

| dialect | family cov | name adds | total |
|---|---|---|---|
| snowflake | 346 | +11 | 357 |
| tsql | 166 | +3 | 169 |
| bigquery | 285 | +6 | 291 |
| duckdb | 418 | +173 | 591 |
| postgres | 189 | +129 | 318 |
| spark | 187 | +118 | 305 |
| databricks | 64 | +103 | 167 |
| trino | 71 | +74 | 145 |
| hive | 350 | +74 | 424 |
| mysql | 381 | +68 | 449 |
| presto | 183 | +52 | 235 |
| redshift | 65 | +39 | 104 |
| starrocks | 47 | +22 | 69 |
| drill | 8 | +22 | 30 |
| dremio | 17 | +14 | 31 |

- Small dialects (clickhouse, oracle, sqlite, teradata, athena, doris, fabric, risingwave, singlestore, ...) add <= 15, and a few add 0.
- The name widening is mostly a legitimate "tests that use this dialect" signal. The families are wide for hive and mysql because helper imports count: Presto uses MySQL.TIME_FORMAT, and Athena imports Hive.
- Class-body edits genuinely need the wider set, since any method may read the attribute.

**F5. The large waste in class-body edits is the fixture-stem substring match, not the family.**
- `fixture_tests` (selector.py:338) and `name_tests` look for `stem in s` as a substring over test strings.
- `optimizer.sql` (stem "optimizer") matches `/optimizer/tpc-ds/` and `optimizer/tpc-ds/tpc-ds.sql`. That pulls test_execute_tpcds (21.5 s), tpch, test_tpcds and others.
- `canonicalize` and `qualify_columns` match 167 tests through strings such as `canonicalize_table_aliases`.
- The real consumers use the exact string. `check_file(file)` loads `optimizer/{file}.sql`, so the argument is exactly "optimizer", "qualify_columns", "canonicalize" (tests/test_optimizer.py:214-226, line 301).
- Fixture parts of the dialect selections:

| dialect | substring match | exact-token match |
|---|---|---|
| snowflake / bigquery / duckdb / mysql / postgres / spark | 231 tests, 51.5 s | 7-13 tests, ~5 s |
| clickhouse / presto / redshift / sqlite / starrocks / oracle | 167 tests, 19.8 s | 1-5 tests, 1.4-4.1 s |

- This is why 4d63 (snowflake Tokenizer constant, failed nothing) selected tpcds. Total modelled, snowflake: 561 tests / 63.9 s sum.

**F6. `all_dialect_tests` always adds generator_memory (6.7 s).**
- Every dialect-dir import-time edit gets it (selector.py:405-411).
- generator_memory only instantiates each dialect's generator 1000 times and measures leaks (tests/test_generator.py:19-38). From parsers/, typing/ or Tokenizer/Dialect attribute edits it can only fail if the import breaks.
- A constant-only edit (the AST is equal once every `Constant` is normalised) cannot break an import in practice.
- The other five `all_dialect_tests` are cheap, so keeping them preserves an import-break canary (test_enum loads every dialect).
- Sample witnesses: 4d63 (Snowflake Tokenizer constant), 535z (`WEEK_OFFSET = -2`), and gbav (`target=True` -> `False`) all include generator_memory. 535z and gbav have no other slow test.

**F7. Scalar class attributes: reader lines intersected with dialect users.**
- For an attribute such as `WEEK_OFFSET`, the tests that can fail run a line that reads the attribute (outside class-body assignments) and use the dialect.
- Check on 535z (tsql `WEEK_OFFSET -1 -> -2`, which really fails test_simplify):
  - Readers: 10 covered-line sets (generator.py:6768, simplify.py:545/561, dialect.py:2287...).
  - Users of {tsql, fabric}, including fixture-driven tests: 164 tests.
  - Intersection: 7 tests, 0.86 s, and it contains test_simplify.
  - Today's selection for that commit is 171 tests.
- The same method on `NUMERIC_ESCAPES` (4d63) is useless. Tokenizer metaclass derivations make the reader set huge (1571 tests). So this rule needs a guard (see R4).

## (b) Savings on the sample and recall risk

| Commit | Today | After R1-R3 | Saving |
|---|---|---|---|
| fryt (R1) | sel 37, cost 9.2 | ~3 tests, ~2.5 | ~7 s |
| 4d63 (R2 + R3) | sel 561, cost 27.5 | ~361 tests, 11.1 s sum, no tpcds | ~18-21 s |
| 535z (R3) | cost 11.2 | ~5 | ~5-6 s |
| gbav (R3) | cost 10.1 | ~3.5 | ~6 s |
| R1-R3 total | | | ~36-40 s |
| ary6, j6uz, rszg (already coverage-only) | | | 0 |

- R4 adds roughly 3-5 s on 535z-like commits.
- The miss patterns are listed per recommendation in section (d).

## (c) Code changes

**tools/build_index.py** (around line 211, the run-once block). Replace the prefix rule:

```python
DIALECT_PREFIXES = ("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/", "sqlglot/typing/")
...
once = bool(deco_names & {"cache", "lru_cache", "cached_property", "cache_info"})
# `_build*` marks lazily built tables (generator._build_dispatch). In dialect files the same prefix
# names parse-time builders and their `_builder` closures, which run per call, not once.
once = once or (name.startswith("_build") and not rel.startswith(DIALECT_PREFIXES))
```

Factories still count as import-time through the module-level-call seed rule, with nested defs excluded.

**selector.py**, `fixture_tests` (line ~338). Match whole tokens first and fall back to the substring only when nothing matches:

```python
def fixture_tests(self, fixture_rel: str) -> set[int]:
    stem = Path(fixture_rel).stem
    memo = self.__dict__.setdefault("_fx_memo", {})
    if stem not in memo:
        def named(s: str) -> bool:
            return s in (stem, stem + ".sql") or s.endswith(("/" + stem, "/" + stem + ".sql"))
        exact = {n for n, strings in self.strings.items() if any(named(s) for s in strings)}
        memo[stem] = exact or {n for n, strings in self.strings.items() if any(stem in s for s in strings)}
    return set(memo[stem])
```

Apply the same `named()` rule to the first `hits` pass in `fixture_change` (line ~634). That is a fixture-file edit and belongs to another report; note it there.

**selector.py**, constant-only edit. Add a helper:

```python
class _ZeroConsts(ast.NodeTransformer):
    def visit_Constant(self, node):
        return ast.copy_location(ast.Constant(value=0), node)

def const_only(old_tree, new_tree) -> bool:
    d = lambda t: ast.dump(_ZeroConsts().visit(copy.deepcopy(t)))
    return d(old_tree) == d(new_tree)
```

In `sqlglot_change` compute `ck = rel.startswith(("sqlglot/parsers/", "sqlglot/typing/", "sqlglot/dialects/")) and const_only(old_tree, new_tree)`. Pass `ck` into `import_time_change` and key the memo on it. In `_import_time_change`, only the `all_dialect_tests` loop changes:

```python
for tid in self.index.get("all_dialect_tests", []):
    if skip_slow and tid.endswith("test_generator_memory"):
        continue
    ...
```

Do not subtract from `hit` itself. Do not apply this to `sqlglot/generators/` edits.

**selector.py**, optional (R4). In the module-level branch of `sqlglot_change`, a changed line is `NAME = <Constant|-Constant>` in a dialect class where `NAME` is fully uppercase and matches `^[A-Z_]+$`. Compute:

```python
readers = lines in sqlglot/**/*.py (any file) matching \bNAME\b that are not class-body assignments to NAME
hit = cov(readers) & (cov(own family files, excluding import-time-only sets) | name_tests(words))
```

Take it only when:
- the reader line count is at most ~40, and
- no reader line is in a metaclass or import fn, and
- NAME is not referenced inside `tokens.py` or `dialect.py` class-creation code.

Otherwise keep the wide rule. The reader table goes in `build_index.py` as `attr_readers[NAME] = [(rel, line)]`.

## (d) Ranked recommendations

1. **R1, `_build*` prefix fix in build_index.py.** ~7 s on the sample (fryt), and likely 5-10 s per affected hidden commit.
   - Risk: low.
   - Miss pattern: a real run-once `_build_*` helper in dialect files that fills a class-level cache. None exists in the pinned source (grepped `def _build` in sqlglot/).
   - Dialect-file index entries drop from 148 to ~30.
2. **R2, exact-token fixture matching.** ~18-20 s on 4d63-type commits (any class-body edit in a dialect with an optimizer.sql header: snowflake, bigquery, duckdb, mysql, postgres, spark).
   - Risk: low-medium.
   - Miss pattern: a test that reads the fixture through a computed name or a directory loop, so no string equals the stem. The fallback to the substring covers the "no exact hit" case, but not a partial one.
   - One pre-check: confirm that each of the 15 fixtures in `fixture_dialects` has at least one exact hit. The listing above shows 1-13 for each.
3. **R3, drop generator_memory for constant-only edits in parsers/, typing/, dialects/.** ~4-6 s per such commit; 3 of 30 on the sample (4d63, 535z, gbav).
   - Risk: low.
   - Miss pattern: a constant edit that raises at import or class creation. Examples: `re.compile("(")` at module level, an int-only tuple-width mismatch in Tokenizer QUOTES, or a constant that makes the `_Dialect.__new__` precomputation throw.
   - test_enum and the other cheap `all_dialect_tests` stay as the canary.
4. **R4, scalar-attribute reader rule.** ~3-5 s on commits like 535z (171 -> 7 tests; the failing test_simplify is retained).
   - Risk: medium.
   - Miss patterns: the attribute is read through `getattr`, an f-string name, or copied at class creation into a derived structure (tokenizer metaclass tables, `Dialect.__new__` tries). Those hide the reader.
   - Keep the guards above, or defer it.
5. **Do not narrow the family walk or the name widening for method edits.** Coverage already covers subclass dialects (F3). Method edits never use the family path (F1). Zero savings and non-zero risk.
6. **Possible follow-up, not done here.** The family walk's helper-import edges make hive and mysql families 29 files. Those edges are real dependencies (Presto uses MySQL.TIME_FORMAT), so leave them.
