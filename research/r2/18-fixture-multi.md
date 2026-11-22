# r2/18 - fixture commits and multi commits

No code was run. Everything below comes from reading `selector.py`, `maps/impact.json.gz` (`test_strings`) and `work/sqlglot` with small inspect-only one-liners.

## (a) Findings

### F1. Direct fixture edits are already tight on the sample, with no recall hole in the readers
- `fixture_change` (selector.py:628-640) matches `stem in s` over every string of every test. All 8 sample fixture commits resolve to 1 test (`why.log`: `fixture-stem:1`).
- Reader audit on the pinned source:
  - `tests/helpers.py:34-55` is the only fixture loader. Besides it there are direct reads in `test_jsonpath.py` and `test_executor.py:30-31,118-119`.
  - No test uses `listdir`, `glob` or `iterdir` on `tests/fixtures` (grep of `tests/**/*.py`). So a created fixture file cannot be picked up by a pinned reader.
  - Every `check_file` and loader call site has a literal first argument. I checked this with `ast`: 26 literal keys, 0 dynamic.
- Parametrisation: all fixture loops use `subTest` inside one test id (`test_optimizer.py:225`, `test_executor.py:157-162`). One fixture line maps to one test id, so there is no per-line selection to do.
- `identity.sql` has exactly 2 readers: `test_transpile::test_identity` and `test_serde::test_serde`. `pretty.sql` has 1 (`test_pretty`), and `partial.sql` has 1 (`test_partial`).
- A literal-string rule finds the true reader set for all 26 `.sql` fixtures. I checked each fixture's enclosing test function from the AST against the map. In the rule below, `exact(...)` means a string equals the stem, equals `parent/stem`, or contains the file name with its suffix:
  ```
  exact(optimizer.sql)=test_optimize (+2 test_sqlglotc, harmless), exact(qualify_columns.sql)=test_qualify_columns,
  exact(canonicalize.sql)=test_canonicalize, exact(cts.json)=test_cts_file, exact(identity.sql)=the 2 above, ...
  ```
  `exact` is a subset of the current loose rule (`stem in s`), so it can only shrink the set. A fallback to the loose rule keeps it safe when `exact` is empty.

### F2. Main finding: the fixture-dialect path in `name_tests` uses the loose rule and drags in 167-231 tests
- `name_tests` (selector.py:274-278) calls `self.fixture_tests(fx)` for every fixture whose `# dialect:`, `# read:` or `# write:` header names a changed dialect word. `fixture_tests` (338-344) uses `stem in s`.
- The loose rule is bad for three stems, because `check_file`'s own helper constants land in every `TestOptimizer` test's `test_strings`:
  - `optimizer` matches `"optimizer/"` and `"/optimizer/tpc-ds/"`, giving 231 tests: 167 `TestOptimizer`, 60 `TestExecutor` (including `test_execute_tpcds` at 21.5 s), 3 `TestSQLGlotC` and 1 other.
  - `qualify_columns` matches `"validate_qualify_columns"`, giving 167 tests.
  - `canonicalize` matches `"canonicalize_table_aliases"`, giving 167 tests.
- These fixtures carry dialect headers. The `fixture_dialects` index (build_index.py:264-278) maps these words to them:

  | dialect word | fixtures | tests selected now (loose) | tests with `exact` |
  |---|---|---|---|
  | snowflake | 11 | 231 | 13 |
  | spark | 10 | 231 | 11 |
  | bigquery | 10 | 231 | 12 |
  | duckdb | 10 | 231 | 12 |
  | postgres | 8 | 231 | 10 |
  | mysql | 5 | 231 | 7 |
  | redshift | 5 | 167 | 5 |
  | clickhouse | 4 | 167 | 4 |
  | presto | 4 | 167 | 4 |
  | sqlite | 3 | 167 | 3 |
  | starrocks | 2 | 167 | 2 |
  | oracle | 1 | 167 | 1 |
  | risingwave | 1 | 167 | 1 |

- `_import_time_change` (selector.py:392-396) calls `name_tests` for any import-time edit under `DIALECT_DIRS`: module level, class body, or an import-time function. That is the "module" or "import-fn" reason in `why.log`.
- Evidence on the sample: 4d63 (`snowflake.py:module`, 561 tests, cost 27.5 s) lists `tpcds 21.5` among its biggest tests.
  - `snowflake.py`'s own coverage never touches `test_execute_tpcds`. I read the union of all its line sets, and `tpcds in it` is False.
  - The only route to that test is `name_tests({"snowflake"})` -> `optimizer.sql` -> the 60 executor tests (the `optimizer/tpc-ds/` strings in `setUpClass`).
  - For comparison, `tsql.py`, `singlestore.py` and `dremio.py` coverage does not touch tpcds either. Their words are not in the table above (tsql 2 tests, singlestore and dremio none), which is why 535z (11.2 s), gbav and fryt do not pay 21.5 s.
- The optimizer fixtures with dialect headers are read only by their one test. The 1-test-per-fixture reader table (F1) is what `exact` encodes. So a dialect edit really needs only those readers (about 13 for snowflake) from this path.

### F3. Hole-free items I checked
- `sql_comment_only` (selector.py:791-801) is at least as strict as `_filter_comments` (`helpers.py:11-12`). It requires `old` and `new`, so created and deleted files are excluded. Whitespace, CR and Unicode separators are conservative. Sound.
- `tpc-ds.sql` has 57 `# execute: true` lines and `tpc-h.sql` has none. Beyond content, a structural edit can raise in `_extract_meta` (`helpers.py:15-24`): a `#` line with no colon (ValueError), or a statement left with only `#` lines (IndexError). That fails `setUpClass`, which fails every `TestExecutor` test. The loose rule selects all 61 tests for any tpc edit, so it is safe. Leave it.
- Binary `csv.gz`: `parse_patch` now fills the path from the `git_old` and `git_new` header paths (selector.py:100-106). Because `tests/` touches code, `select` routes it to "all tests" (selector.py:652-653). Safe, but 103 s. It is rare, and `TestExecutor` (60 tests, about 31 s) would suffice.
- Fixture renames go to "rename" -> all tests (selector.py:654-656). Safe.
- A deleted fixture still looks up its old path, because `f["path"] = new or old`. Safe.
- Created fixture with no hit (fixture-new): the stem finds nothing, so the fallback is parent name `optimizer` -> 231 tests (51 s), or `fixtures` -> everything. That is waste only. Pinned tests cannot read a file that did not exist, and a reader added in the same patch comes from the test `.py` change. A new test id is not in `tests.json` anyway.

### F4. Multi commits combine correctly
- `select` (selector.py:642-673) takes the union (`out |= s`) over per-file results. There is no early exit or shortcut that drops a part. Nothing to fix.
- The only multi-related cost is F2: a fixture or dialect part inflates the union. `986c`, `bvw2` and `rszg` are fine: their fixture parts are 1 test and their code parts are `ast-identical` or small.
- Comment-only fixture edits give `set()` and union cleanly with the other parts.

## (b) Savings and risk
- Sample: 4d63 drops `test_execute_tpcds` (21.5 s) and 166 or so other optimizer and executor tests. Estimated cost goes from 27.5 s to about 8-9 s (generator_memory 6.7 s becomes the floor), so about -18 s.
- No other sample commit goes through a fixture-dialect word with 231 or 167 tests. 535z, gbav, fryt, ary6, j6uz and the clickhouse commits are unaffected or already small.
- Hidden set (about 45 commits): each import-time or module-level edit in snowflake, bigquery, duckdb, spark, postgres or mysql would save the same 15-20 s. Expect 2-4 such commits, so about 40-70 s on the hidden score.
- Recall risk of `exact` is low. `exact` is a subset of loose, so it only misses a test when a reader names its fixture without any of: an equal-to-stem literal, a `parent/stem` literal, or a literal containing `<stem>.<ext>`. That means a reader that assembles the name from fragments. All 26 pinned call sites have literal arguments. Patch shapes that could miss: none on the pinned test code. A hypothetical patch that adds such a reader also edits a test `.py`, which is selected separately.
- Fallback: when `exact` is empty, use the old loose rule, so unknown names never become empty selections.

## (c) Exact code changes (selector.py)

1. Add a memoised exact reader lookup next to `fixture_tests`, and make `fixture_tests` use it. Replace selector.py:338-344:
```python
    def fixture_readers(self, fixture_rel: str) -> set[int]:
        """Tests that name the fixture file itself: a string equal to its stem, to parent/stem
        (check_file("tpc-h/tpc-h")), or containing its file name ("identity.sql", "cts.json").
        A subset of the loose `stem in s` match: check_file's helper constants ("optimizer/",
        "canonicalize_table_aliases") no longer drag in every test of the class."""
        memo = self.__dict__.setdefault("_fx_exact", {})
        if fixture_rel not in memo:
            p = Path(fixture_rel)
            stem, name, par = p.stem, p.name, p.parent.name
            memo[fixture_rel] = {n for n, strings in self.strings.items()
                                 if any(s == stem or s == f"{par}/{stem}" or name in s for s in strings)}
        return set(memo[fixture_rel])

    def fixture_tests(self, fixture_rel: str) -> set[int]:
        """Tests reading the fixture file (loose stem match when no test names it exactly)."""
        hits = self.fixture_readers(fixture_rel)
        if hits:
            return hits
        stem = Path(fixture_rel).stem
        memo = self.__dict__.setdefault("_fx_memo", {})
        if stem not in memo:
            memo[stem] = {n for n, strings in self.strings.items() if any(stem in s for s in strings)}
        return set(memo[stem])
```

2. Use the same lookup for direct fixture edits, including the old name of a renamed or deleted file. Replace the stem and dir part of `fixture_change` (selector.py:633-640):
```python
        hits: set[int] = set()
        for p in dict.fromkeys(x for x in (f["new"], f["old"], f["path"]) if x):
            hits |= self.fixture_readers(p)
        if hits:
            return hits, "fixture-exact"
        if f["created"] and not f["deleted"]:
            # Pinned tests only open fixtures by literal name and never list the directory, so a
            # brand-new file has no reader; a reader added in the same patch is a test .py change.
            return set(), "fixture-new"
        stem, parent = path.stem, path.parent.name
        hits = {n for n, strings in self.strings.items() if any(stem in s for s in strings)}
        if hits:
            return hits, "fixture-stem"
        hits = {n for n, strings in self.strings.items() if any(parent in s for s in strings)}
        if hits:
            return hits, "fixture-dir"
        return set(self.all), "fixture-unmapped"
```
For tpc-h and tpc-ds `.sql` this still returns all 61 tests (via the `setUpClass` strings), which is correct (F3). For `*.csv.gz`, `exact` finds nothing, so the old stem/dir fallbacks run and give the 61-test dir match.

3. Optional, tiny: narrow binary `tests/fixtures/optimizer/tpc-*/*.csv.gz` from "all" to `fixture_change`'s dir fallback. In `select`, change the binary branch (selector.py:652-653) to:
```python
            elif f["binary"]:
                if rel.startswith("tests/fixtures/optimizer/tpc-"):
                    s, r = self.fixture_change(f)          # TestExecutor + test_tpch/test_tpcds (61 tests)
                else:
                    s, r = (set(self.all), "binary") if touches_code else (set(), "binary-other")
```
It saves about 70 s of cost on a rare patch. Risk is low because `setUpClass` is in the dir match. I would skip it if you want to stay minimal.

## (d) Ranked recommendations

| # | Change | Saves (sample / hidden) | Risk |
|---|---|---|---|
| 1 | Change 1: exact-match `fixture_tests` for the fixture-dialect path | about 18 s on 4d63; about 15-20 s per hidden snowflake, bigquery, duckdb, spark, postgres or mysql module-level edit | Low (subset of loose rule; falls back to loose when empty; all 26 pinned readers are literal) |
| 2 | Change 2: exact-match `fixture_change` plus the old path of a rename or delete. It is neutral on the sample, where all 8 fixture commits already resolve to 1 test. | 0 s on the sample. In the hidden set, one `optimizer.sql` edit saves about 50 s of test time, and one `qualify_columns.sql` or `canonicalize.sql` edit about 20 s; `cts.json` is rare. | Low |
| 3 | Created fixture -> `set()` | 0 s on the sample; saves 51-103 s of test time on any new fixture patch | Low (no globbing readers; same-patch readers come from the `.py` change) |
| 4 | Binary csv.gz narrowing | rare, about 70 s of test time | Low-Medium; skip if unsure |

Keep as is:
- The comment-only rule is sound.
- The `union` in multi commits is correct.
- The tpc `.sql` rule at 61 tests is correct, because of `_extract_meta` failures in `setUpClass`.
