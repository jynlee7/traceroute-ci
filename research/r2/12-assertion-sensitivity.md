# R2-12: Assertion sensitivity (what a test would notice, not just what it executes)

Method: read-only AST passes over `work/sqlglot/tests/**/*.py` (the `ast` module on source text), plus read-only lookups in `maps/impact.json.gz`, `work/index.json` and `work/why.log`. Nothing was run. The scratch scripts are in the session scratchpad and are not deliverables.

## Headline

Classifying tests by observation channel gives **little by itself**, because 88 % of the suite observes generated SQL. Coverage already separates the parse-only tests from the generator lines.

The useful output of this pass is three things:
1. A measured picture of the channels (section F1-F3).
2. A **bug in `tools/build_index.py`** found while tracing `fryt`. It makes every `_build*` function and every `_builder` closure count as run at import time, which over-selects 123 functions (F4).
3. A **gate for `test_generator_memory`** on scalar-only class-body edits (R1).

Estimated sample saving: about 12-17 s from items I own (R1 + R2), plus about 18 s from F9 of report 11, which I confirmed as the mechanism behind `4d63`. Hidden-set shape risk is stated per rule below.

## (a) Findings

### F1. Channel census (static, over all 1620 tests in `sample/tests.json`)

For each test I took the call closure: its own body plus same-file helpers and `Validator` methods reached by name.

| Channel (derived from calls in the closure) | Tests | Seconds |
|---|---|---|
| GEN: calls `.sql`, `transpile`, `generate`, `validate_identity`, `validate_all`, `validate_transpile`, `assert_duckdb_sql`, or a library entry that generates internally (`optimize`, `qualify`, `simplify`, `execute`, `lineage`, `MappingSchema`, `build_scope`, ...) | 1424 | 66.0 |
| STRLIKE: no GEN call, but `str()`, `repr()`, f-strings, `assertLogs` (can render an expression) | 39 | 29.2 (almost all of it `test_max_nodes`, 28.8) |
| BLIND: no GEN call and no stringification (pure tree, token or raise checks) | 157 | 7.8 (6.7 of it `test_generator_memory`, which I count as BLIND because it never calls `.sql`) |

Per-test text channel, by evidence in `tests/dialects/test_dialect.py`:
- `validate_identity` (lines 51-61) asserts `parse(sql).sql(dialect) == sql`.
- `validate_all` (lines 74-110) asserts exact equality for every `read` and `write` entry (plus `UnsupportedError` raises).
- `validate_transpile` (lines 63-67) asserts exact equality.

So a dialect test observes **any** change to the parse, tree or generate path of the statement on its line. Nothing can be pruned inside the GEN class by looking at assertions.

### F2. Coverage already separates the layers

Coverage-set composition of some hot files, by channel:

| File | Tests covering it | GEN | STRLIKE | BLIND |
|---|---|---|---|---|
| `sqlglot/generator.py` | 1353 | 1345 | 2 (0 s) | 6 (6.7 s, i.e. `test_generator_memory` only) |
| `sqlglot/generators/clickhouse.py` | 100 | 99 | 1 | 0 |
| `sqlglot/executor/python.py` | 53 | 53 | 0 | 0 |
| `sqlglot/optimizer/simplify.py` | 132 | 130 | 0 | 2 (0 s) |
| `sqlglot/parser.py` | 1519 | 1410 | 20 (29.2 s) | 89 (0.6 s) |

Reading this table:
- `test_max_nodes` never covers generator lines, so for a function-body edit in the generator, "a generator change only matters to tests that compare generated SQL" is **already true by coverage**.
- A channel gate on exact line hits would remove about 6.7 s (`test_generator_memory`) and nothing else. Do not build a general gate for that.
- The only place where channel information is not already implied is the **widened** sets (import-time edits, fallbacks). There the selector unions whole-family coverage, name tests and `all_dialect_tests`.

### F3. Where the slow tests sit by channel

- `test_max_nodes` (`tests/test_parser.py:1567-1591`): parse-only plus raises. It cannot see generator, optimizer or executor edits. Already handled by exact coverage (it is not selected in any sample commit's top list).
- `test_execute_tpch` (`tests/test_executor.py:187-188`): parse plus pickle only. Report 11 F2 and C1 cover narrowing it. No change from me.
- `test_execute_tpcds`: channel is the **result set** compared with duckdb. Of the 4 sample commits whose exact coverage hits it (2jrq, 3utz, 66ks, 2haz), 2 fail it. P(fail | executed) is therefore about 50 %, so no channel prior can prune it.
- `test_generator_memory` (`tests/test_generator.py:19-38`): a PERF channel. It observes only `Generator.__init__`, `_build_dispatch` and per-instance leaks, plus "every dialect imports and constructs".

### F4. Bug: the `_build*` name prefix makes 123 ordinary functions "run at import"

- Location: `tools/build_index.py:~232`, in the run-once detection: `once = once or name.startswith("_build")`.
- It was meant for `generator._build_dispatch` (`sqlglot/generator.py:74`). It also matches every `_builder` closure and every `_build_*` parse builder.
- Count: 123 `def _build...` in `sqlglot/` (grep). Examples: `parsers/tsql.py` (`_build_datetrunc`, `_build_format`, ...), `parsers/sqlite.py` (`_build_min`, `_build_date`, ...), `parsers/snowflake.py` (`_build_trunc`, ...), and 7 `_builder` closures in `dialects/dialect.py`.
- The real run-once helpers are only these:
  - `optimizer/annotate_types.py:119` `_build_coerces_to`, called at module level at line 157. That is already a module-level call seed.
  - `optimizer/simplify.py:1978` `_build_gen_dispatch`, called at line 1988. Already a seed.
  - `generator.py:74` `_build_dispatch(cls)`. It takes `cls` and is the only one needing the heuristic.
- Evidence of harm: `work/index.json` `import_fns["sqlglot/parsers/dremio.py"] = [[38,55,[]],[33,57,[[38,55]]]]`. `_builder` (lines 38-55) is an entry of its own, so the nested-def exclusion cannot protect it.
- Consequence for `fryt` (`parsers/dremio.py:39`, `len(args) == 2` flipped inside the `_builder` closure): `why.log` says `import-fn:37`. The `import_fns` branch (`selector.py:~280`) unions `import_time_change` (family `file_cov`, name tests, class tests, `all_dialect_tests`, so `test_generator_memory` 6.7 s) with the function's coverage. The cost is 9.2 s for a closure that runs only when a Dremio `DATE_ADD` or `DATE_SUB` is parsed.
- Hidden-set exposure: any single-token mutation inside any of those 123 functions takes this path. In the 1.5x set I expect about 2-4 such commits.

### F5. Mechanism behind `4d63` pulling `test_execute_tpcds` (confirms report 11 F9)

- `4d63` flips a Snowflake `NUMERIC_ESCAPES` constant: 561 tests selected, with tpcds as a one-test worker.
- The `file_cov` of the three Snowflake files has no tpcds, no max_nodes and no memory test (0 of 229, 272 and 227 tests).
- Path: `fixture_dialects["snowflake"]` (`work/index.json`) lists 11 optimizer fixtures, including `tests/fixtures/optimizer/optimizer.sql`. `name_tests` then calls `fixture_tests` (`selector.py:~262`), which tests `stem in s`. The stem `optimizer` is a substring of `/optimizer/tpc-ds/` in `test_execute_tpcds`'s strings.
- Confirmed on the map: tpcds holds the strings `/optimizer/tpc-ds/`, `/optimizer/tpc-h/` and `optimizer/tpc-ds/tpc-ds.sql`.
- Dialect-affinity is otherwise sound for a different reason: `sqlglot/dialects/__init__.py` loads dialects lazily (`__getattr__` over `MODULE_BY_ATTRIBUTE`), and nothing under `sqlglot/` outside the dialect directories loads Snowflake except by string. A test sees a dialect module only if it names the dialect, reads a fixture that names it, or loops over every dialect.

### F6. Shape of the sample mutations

The sample mutations are single-token flips: comparison operators, int and bool constants, and argument swaps. Examples: `tsql.py` `WEEK_OFFSET = -1` to `-2`, `singlestore` `target=True` to `False`, `ddl.py` `"constraint": False` to `True`, snowflake `0x...` base `16` to `17`.

Class-body flips of a bool or int constant cannot raise at class creation unless something validates the constant at import. This is the basis of R1.

## (b) Savings and risk

| Item | Sample saving | Hidden-set risk |
|---|---|---|
| R1 gate `test_generator_memory` and `all_dialect_tests` on scalar-only edits | about 10 s (535z about -5, gbav about -5; 4d63 about -1, since its worker is short) | Low. See miss shapes below. |
| R2 fix the `_build*` heuristic | about 5-6 s (fryt 9.2 to about 3) | Low |
| R3 sink index (optional, to explain and gate widened sets) | 0-1 s | Low, but adds code. Not recommended now. |
| F9 stem fix (report 11, confirmed here) | about 18 s (4d63) | Low |

Patch shapes that would cause a miss:
- R1: a bool or int change in a class body consumed **at import** by a validating call, such as `re.compile`, `range`, an index, a shift or a division in `Tokenizer.__init_subclass__` (`tokens.py:85-124`) or `Dialect.__new__` (`dialect.py:307-432`), which raises for one dialect. Then every test that loads that dialect fails, and so does `test_generator_memory`, which is the one missed. I estimate at most 0.1-0.5 % per scalar-only commit. The break-even P(fail) for `test_generator_memory` in report 11 is 0.4-2.2 %, so the rule is positive but near the margin. Hence it is restricted to bool, int and float, never str or None.
- R1 also never applies to an edit inside `Generator.__init__`, `_build_dispatch` or `Dialect.generator`. Those are function-body edits, which stay on the coverage path where `test_generator_memory` is exact.
- R2: a genuinely run-once `_build*` helper outside the three known ones. The `cls` first-argument rule plus module-level call seeds cover the three. A new hidden helper of this kind does not exist at the pin (the pin is fixed), so the risk is only that my grep missed one. Verify with a comparison of `import_fns` before and after at build time (see section c).

## (c) Code changes

### C1 (R2). `tools/build_index.py`, run-once detection (`~line 232`)

Replace the name-prefix test with a signature test. `_build_dispatch(cls, ...)` is a module-level function that fills a class-keyed cache. The other two are caught as module-level call seeds.

```diff
-                once = bool(deco_names & {"cache", "lru_cache", "cached_property", "cache_info"})
-                once = once or name.startswith("_build")
+                once = bool(deco_names & {"cache", "lru_cache", "cached_property", "cache_info"})
+                # Module-level helpers taking the class (generator._build_dispatch(cls)) run once per
+                # class and are cached. Do not match by name prefix: 120+ parse builders and `_builder`
+                # closures are called on every parse.
+                first = d.args.args[0].arg if d.args.args else ""
+                once = once or (d.col_offset == 0 and first == "cls")
```

Build-time check to add right after `import_fns` is built (print to stderr, no failure): the number of entries before and after, and assert that `sqlglot/generator.py` still lists `_build_dispatch`.

### C2 (R1). `selector.py`: scalar-only class-body edits do not widen to `all_dialect_tests`

Add a helper next to `bytes_inert` (`~line 766`):

```python
def scalar_only(old_tree, new_tree) -> bool:
    """True when the trees differ only in the VALUE of bool/int/float constants of the same type.
    Such an edit cannot change structure, imports or names; it cannot raise at class creation
    unless something validates the number at import, which is rare."""
    class Mask(ast.NodeTransformer):
        def visit_Constant(self, node):
            if isinstance(node.value, (bool, int, float)):
                return ast.copy_location(ast.Constant(value=type(node.value).__name__), node)
            return node
    def dump(t):
        return ast.dump(Mask().visit(ast.parse(ast.unparse(t))))
    try:
        return old_tree is not None and new_tree is not None and dump(old_tree) == dump(new_tree)
    except Exception:
        return False
```

(`ast.unparse` first so the masking never mutates the trees reused later.)

In `sqlglot_change` (`~line 495`), compute it once and pass it to the import-time path:

```diff
         changed = changed_statement_lines(old_tree, new_tree, touched_old_lines(f))
+        scalar = scalar_only(old_tree, new_tree)
...
-                hit = self.import_time_change(rel, ln, renamed)
+                hit = self.import_time_change(rel, ln, renamed, scalar)
```

and likewise at the `in_import_fn` call (`~line 497`, the one before `self.cov(rel, range(fn[0], fn[1] + 1))`).

Change the memo key and the signature (`~line 373`):

```diff
-    def import_time_change(self, rel: str, line: int, extra_names: set[str] = frozenset()) -> set[int]:
-        key = (rel, self.top_class(rel, line), frozenset(extra_names))
+    def import_time_change(self, rel: str, line: int, extra_names: set[str] = frozenset(), scalar: bool = False) -> set[int]:
+        key = (rel, self.top_class(rel, line), frozenset(extra_names), scalar)
         ...
-            memo[key] = self._import_time_change(rel, line, extra_names)
+            memo[key] = self._import_time_change(rel, line, extra_names, scalar)
```

and in `_import_time_change` (`~line 405`):

```diff
-        if rel.startswith(DIALECT_DIRS) or rel == "sqlglot/dialects/dialect.py":
+        if (rel.startswith(DIALECT_DIRS) or rel == "sqlglot/dialects/dialect.py") and not scalar:
             for tid in self.index.get("all_dialect_tests", []):
```

Keep `file_cov(family)`, `name_tests` and `class_tests` unchanged: they are the real consumers of the constant (the dialect's own tests).

Restrict the gate to dialect files only; for `tokens.py`, `generator.py`, `parser.py` and `dialect.py` keep the old behaviour (shared by every dialect, so a bad number there is more dangerous). Add `and rel.startswith(DIALECT_DIRS)` to the `scalar` argument if you want that: `scalar = scalar_only(...) and rel.startswith(DIALECT_DIRS)`.

### C3 (optional, R3). `tools/build_index.py`: emit a `sink` map

Only needed if you want the gate to extend to other widened sets. The logic is the `ch2.py` closure walk: for each `test*` function, collect call names (own body plus same-file non-test helpers, one fixed point), then:

```python
GEN = {"sql","transpile","generate","unparse","validate_identity","validate_all","validate_transpile",
       "assert_duckdb_sql","to_s","dump","optimize","qualify","simplify","execute","Executor","lineage",
       "annotate_types","MappingSchema","ensure_schema","build_scope","traverse_scope","normalize","canonicalize"}
STR = {"str","repr","format","print","pformat","assertLogs","assertWarns","<fstr>"}
sink = "GEN" if names & GEN else "STR" if names & STR else "BLIND"
# out["sink"] = {test_id: sink}
```

Usage would be: in `_import_time_change`, for a generator-layer file, drop `BLIND` and `STR` tests from the **widened** part only (never from `self.cov(rel, [ln])` exact hits). Measured value on the sample is about 0 s beyond R1, so I do not recommend shipping C3 now.

## (d) Ranked recommendations

| # | Recommendation | Expected s saved (sample) | Risk |
|---|---|---|---|
| 1 | Report 11 F9: use exact string match (or path-component match) in `fixture_tests` and `fixture_change` instead of `stem in s`. I confirmed it is the only route by which `4d63` selects tpcds, and that `fixture_dialects["snowflake"]` lists 11 optimizer fixtures. Do this first. | about 18 | Low (a stricter match, but test strings that hold the fixture file name stay selected) |
| 2 | C1: replace `name.startswith("_build")` with the `cls`-first-argument rule. 123 functions stop counting as import-time. | about 5-6 (fryt) | Low |
| 3 | C2: scalar-only class-body edits in dialect files skip `all_dialect_tests` (and so `test_generator_memory`). | about 10 | Low-medium (near break-even, restricted to bool, int, float) |
| 4 | Do **not** build a generator-sink gate on exact line hits. Coverage already excludes the parse-only tests (F2), and the remaining BLIND tests are `test_generator_memory`, which R1 covers. | 0 | n/a |
| 5 | Do **not** add a result-channel prior to prune tpcds. P(fail given executed) is about 50 % on the 4 sample commits that execute it. | 0 | n/a |
| 6 | C3 sink index, only if later rules need it. | 0-1 | Low |

Interactions: 1 and 3 overlap on `4d63` (the F9 fix removes tpcds, R1 removes `test_generator_memory`). 2 stands alone. Do not double count in a combined estimate: expect about 28-34 s together on the sample, not 33-40.
