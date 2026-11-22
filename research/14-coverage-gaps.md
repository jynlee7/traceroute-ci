# 14: Per-test coverage blind spots (beyond the process pools)

Scope: `work/sqlglot/tests` searched for subprocesses, threads, multiprocessing, `setUpClass`, module-level
work, and pytest-cov contexts under xdist. Read-only one-liners were run on `maps/impact.json.gz`.
Nothing was executed.

## How contexts get recorded (the root cause of most gaps)

- `tools/coverage.sh:14-15` runs `pytest -n 4 --cov=sqlglot --cov-context=test`. pytest-cov switches the
  dynamic context to `<nodeid>|setup|run|teardown` only around each test phase. Inside each xdist worker:
  - Module imports and collection run under the empty context `""`.
  - unittest `setUpClass` runs inside the *setup* phase of the first test of that class that the worker
    picks up, so it is recorded as `<first test>|setup`. `tearDownClass` lands on the last test.
  - Work that is cached per process (dialect class creation, generator dispatch tables, tries, lazy
    `__getattr__` imports) is recorded only under the **first test in each of the 4 workers** that triggers it.
- `tools/build_map.py:101-102` (`if not ctx: continue`) throws away the empty context. A line that ran at
  import is therefore indistinguishable from "ran in these few tests". Lines that ran *only* at import get the
  empty set.
- `tools/build_map.py:103` (`ctx.rsplit("|",1)[0]`) merges the setup and run phases, so `setUpClass` lines are
  credited only to the first test of the class in each worker.
- `--cov=sqlglot` does not measure `sqlglotc/` at all. Subprocesses are not measured either (no
  `COVERAGE_PROCESS_START`, no `patch=subprocess`).

Map totals (one-liner over `files[*].lines`): 53,696 measured lines, of which **10,900 (20%) have an empty test
set**. They ran only at import or collection time.

## (a) Findings

### A. Tests with no or very little coverage (from `impact.json.gz`)
Tests that appear in **no set at all**:

| test | why |
|---|---|
| `tests/dialects/test_dialect.py::TestDialect::test_lazy_load` | `test_dialect.py:233-238` runs `subprocess.run(["python","-c","import sqlglot; assert len(sqlglot.Dialect._classes) == 1 ..."])`. The child process is not traced. The test really depends on the whole `import sqlglot` closure (`sqlglot/__init__.py:16-40`) staying free of concrete-dialect imports, and on dialect registration in `_Dialect.__new__` (`dialects/dialect.py:306`) / `_try_load` (`:239`). |
| `tests/test_sqlglotc.py::*` (3 tests) | `test_sqlglotc.py:14-27` `setUpClass` runs `runpy.run_path("sqlglotc/setup.py")`. Only `sqlglotc/setup.py` (and setuptools) are exercised, and `--cov=sqlglot` does not measure it. |
| `tests/test_typing.py::TestTyping::test_no_redundant_expression_metadata` | `test_typing.py:54-83` reads module-level `EXPRESSION_METADATA` dicts and parses the typing source text. All of that work happened at import time (empty context). The 2026-11-21 `TYPING_TEST` rule already covers it (`selector.py` `sqlglot_change`, last lines). |

Very small coverage (lines covered, distinct sets):
- `test_dialect_imports.py::test_dialect_import_no_deadlock` / `::test_dialect_attribute_access_is_cached`:
  7 lines, 1 set. That set is `sqlglot/dialects/__init__.py:120-128` (`__getattr__`) only. What the deadlock
  test actually checks is that importing Athena→Trino/Hive under `_import_lock = threading.RLock()`
  (`dialects/__init__.py:117`) re-enters without deadlocking. The import of athena/trino/hive happened earlier
  in the worker, under another test or under `""`. The test uses no threads; the "thread" risk is the lock.
  Its coverage also depends on test order: if an earlier test in the same worker already did
  `from sqlglot.dialects import Athena`, `globals()[name]` (`:126`) short-circuits `__getattr__` and the test
  covers nothing.
- `test_dialect_entry_points.py::*` (14-18 lines): mocks `entry_points`. `setUp`/`tearDown` call
  `Dialect._classes.clear()` (`:13-17`), which also perturbs first-load attribution for later tests in the
  same worker. `FakeDialect(DialectBase)` is defined at module level (`:8`), so its `_Dialect.__new__` run is
  recorded under `""`.
- `test_errors.py::test_highlight_sql_*` (2-26 lines), `test_helper.py::test_name_sequence/to_int/merge_ranges/tsort`
  (2-15), `test_time.py::test_subsecond_precision*` (9-11), `test_executor.py::test_table_to_pylist` (14),
  `test_expressions.py::test_identifier` (17), `test_dialect.py::test_compare_dialects/_versions` (22/35):
  these test pure helpers and are genuinely small. Not a gap.

### B. Shared code whose coverage is "first test per worker" or empty (the dangerous kind)
A **miss** is any failing test not selected (SCORING.md:44-46). These functions break *every* test that relies
on them, but the map credits only a handful:

| code | map coverage | what a bug breaks |
|---|---|---|
| `generator.py:74-89` `_build_dispatch`, called once per Generator class through `_DISPATCH_CACHE` (`:1004-1009`) | **67 tests** (lines 77-89) | all SQL generation (~1,350 tests) |
| `dialects/dialect.py:306` `_Dialect.__new__` (84 body lines) | 65 tests | every test using that dialect |
| `tokens.py:84` `_Tokenizer.__init_subclass__` | 65 tests | all tokenizing of that dialect |
| `dialects/dialect.py:239` `_try_load` | 75 tests | lazy dialect lookup by name |
| `expressions/core.py:105-112` `Expression.__init_subclass__` (runs at import for every class) | **2 tests** (`test_fallback_function_*`) | everything |
| `helper.py:42-51` `AutoName._generate_next_value_` (enum values of `ErrorLevel`, `DType`, `NormalizationStrategy`…) | **empty** (lines 50-51, class range also empty) | everything |
| `helper.py:135-162` `subclasses` → `expressions/__init__.py:49,51` `ALL_FUNCTIONS`, `EXPR_CLASSES`; `typing/__init__.py:63,67`; `generators/python.py:104` | lines 151-160 empty, 162: 331 | parser function table, generator dispatch |
| `optimizer/simplify.py:1978-1989` `_build_gen_dispatch` (module-level `GEN_DISPATCH`) | **empty** | simplify |
| `optimizer/annotate_types.py:115-157` `swap_all`, `_build_coerces_to` (module-level `_COERCES_TO`) | **empty** | type annotation |
| `optimizer/__init__.py:50` lazy `__getattr__` | 8 tests, 2 empty lines | optimizer imports |

One-liner result: 34 function bodies ran *only* under the empty context. Some are `@overload` / Protocol stubs
whose signature lines are counted, which are harmless. The real ones are the empty rows above.

### C. setUpClass attribution
- `test_optimizer.py:110-155` (`ProcessPoolExecutor`, `sqlglot.schema = MappingSchema()`, duckdb).
  `MappingSchema.__init__` is credited to 146 of 167 optimizer tests. A `setUpClass` error fails all 167.
- `test_executor.py:72-120` builds `Table` objects (`sqlglot/executor/table.py:11-20`) for all TPC tables. Lines
  17-20 are credited to 57 of 60 executor tests. The other 3 would still error if `setUpClass` raised.
- `test_lineage.py:13-15` `MappingSchema()` is credited to the first lineage test per worker. It is covered
  widely elsewhere, so the risk is low.

## (b) Recall risks under the current `selector.py`

`sqlglot_change` fn branch (`selector.py:357-366`) never reaches "all tests" for a function edit. If the window,
the whole function and the class all come back empty, it adds **nothing** and moves on.
1. **Edit inside an import-only function body**: `AutoName._generate_next_value_`, `_build_gen_dispatch`,
   `_build_coerces_to`, `helper.subclasses` lines 151-160. The selection is **empty**, or 331 tests for
   `subclasses`, while the whole suite fails. A certain miss.
2. **Edit inside a cache-gated function**: `_build_dispatch`, `_Dialect.__new__`, `_Tokenizer.__init_subclass__`,
   `Expression.__init_subclass__`, `_try_load`. The selection is 2-75 tests; the failures are hundreds. A
   certain miss unless another rule (new-def, module-level) happens to widen it.
3. **Module-level edit in any module in the `import sqlglot` closure** that adds an eager dialect import, e.g.
   `from sqlglot.dialects.duckdb import DuckDB` at the top of `sqlglot/schema.py` or `diff.py`. It goes to
   `import_time_change` → `file_cov`, and `test_lazy_load` is never in any set → miss. (Only package
   `__init__.py` edits are caught, by "package-init".)
4. **Import-order edits in athena/trino/hive** (circular import, lock change inside `__getattr__`): the dialect's
   own tests are selected, but `test_dialect_import_no_deadlock` is selected only through `dialects/__init__.py`
   edits → miss.
5. **`MappingSchema.__init__` / `executor.table.Table.__init__` edits** that raise: the tests that are not first
   in their worker are not selected. These are probably covered anyway by wide coverage elsewhere, but with no
   guarantee.

Cost waste: any `sqlglotc/*` edit goes to "other-python" (`selector.py:345`), which selects all 1,620 tests, slow
tests included (≈57 s floor + load). Only `tests/test_sqlglotc.py` (3 tests) can fail from `sqlglotc/setup.py`.
`sqlglotc/` holds `setup.py`, `pyproject.toml` and `MANIFEST.in`.

## (c) Proposed changes

### C1. `selector.py` fallback (cheap, no map rebuild; do first)
In `sqlglot_change`, after the class fallback (`selector.py:363-364`):
```diff
                 if not hit and cls:
                     hit = self.cov(rel, range(cls[0], cls[1] + 1))
+                if not hit:
+                    table = self.files.get(rel, {}).get("lines", {})
+                    if any(str(x) in table for x in range(fn[0], fn[1] + 1)):
+                        # The function ran, but only at import/collection (empty context): treat it like
+                        # a module-level edit. If nothing claims it, run everything.
+                        hit = self.import_time_change(rel, ln) or set(self.all)
+                        reasons.append("fn-import-only")
                 out |= hit
                 reasons.append("fn")
```
Functions that are absent from the map (never executed) still select nothing, same as today.

### C2. `build_map.py`: keep the import-time signal and broadcast class setup
Replace the context loop at `tools/build_map.py:97-109`:
```python
        lines, imp = {}, []
        for lineno, contexts in data.contexts_by_lineno(measured).items():
            tests = set()
            for ctx in contexts:
                if not ctx:
                    imp.append(lineno)              # ran at import / collection in some worker
                    continue
                tid, _, phase = ctx.rpartition("|")
                if tid in index:
                    tests.add(index[tid])
                    if phase == "setup" and tid.rsplit("::", 1)[0] in class_setup:
                        tests |= class_setup[tid.rsplit("::", 1)[0]]   # setUpClass fails the whole class
                else:
                    unmatched.add(tid)
            lines[str(lineno)] = intern(tests)
        files[rel] = {"lines": lines, "import": sorted(set(imp))}
```
Before the loop, compute `class_setup` (classes that define `setUpClass` or `setUpModule`):
```python
    class_setup: dict[str, set[int]] = {}
    for path in (ROOT / "tests").rglob("test_*.py"):
        rel_t = str(path.relative_to(ROOT))
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.ClassDef) and any(isinstance(i, ast.FunctionDef) and i.name == "setUpClass" for i in node.body):
                pre = f"{rel_t}::{node.name}"
                class_setup[pre] = {i for t, i in index.items() if t.startswith(pre + "::")}
```
A per-test `setUp` runs in every test's setup phase anyway, so the broadcast only widens `setUpClass` lines.
Selector use: in the fn branch, when any line in `[lo, hi]` is in `files[rel]["import"]`, also add
`self.import_time_change(rel, ln)`. This catches `Expression.__init_subclass__`, `subclasses` and `_Dialect.__new__`
for dialects that were imported during collection. Keep `import` as `[start,end]` runs if size matters.

### C3. Cache-gated code: order-perturbation run (catches `_build_dispatch`, `_try_load`, lazy imports)
Add `tools/reverse_order.py`:
```python
def pytest_collection_modifyitems(items):
    items.reverse()
```
Then add to `tools/coverage.sh` a second full run with a different distribution and order:
```sh
COVERAGE_FILE=.coverage.rev PYTHONPATH=.:../../tools python -m pytest -q -p no:cacheprovider -p reverse_order \
  -n 4 --dist loadfile --cov=sqlglot --cov-context=test --cov-report= tests > ../pytest_rev.log 2>&1 || true
```
In `build_map.py`, read `.coverage.rev` separately (not through `data.update`). For each line, union the two
test sets, and mark the line `order_dependent` when the sets differ (sqlglot tests are deterministic, so a
difference means first-use caching). Store `files[rel]["once"] = [...]`. In the selector, treat a changed line
in `once` (or within the window) like import-time: `import_time_change(rel, ln)`.

### C4. Cold per-test runs for the import tests (subprocess, lock, entry points)
Use a static context so that collection and `import sqlglot` are credited:
```sh
cat > /tmp/lazy.py <<'PY'
import sqlglot; assert len(sqlglot.Dialect._classes) == 1
PY
COVERAGE_FILE=.coverage.cold.lazy python -m coverage run --source=sqlglot \
  --context="tests/dialects/test_dialect.py::TestDialect::test_lazy_load|run" /tmp/lazy.py || true
i=0
for t in tests/test_dialect_imports.py::TestDialectImports::test_dialect_import_no_deadlock \
         tests/test_dialect_imports.py::TestDialectImports::test_dialect_attribute_access_is_cached \
         $(python -m pytest --collect-only -q tests/test_dialect_entry_points.py | grep ::); do
  i=$((i+1)); COVERAGE_FILE=.coverage.cold.$i python -m coverage run --source=sqlglot --context="$t|run" \
    -m pytest -q -p no:cacheprovider -p no:cov "$t" || true
done
```
In `build_map.py:74`, change the merge to `for extra in sorted(p.name for p in ROOT.glob(".coverage.*") if not p.name.endswith(".rev"))`.
For `.coverage.cold.*`, keep only module-level and class-body lines and import-only function lines. Otherwise
those 5-6 tests become members of every import-time set. That is correct for them but makes `file_cov` lists
longer. After this, rule 3 in (b) is caught: `import_time_change` → `file_cov(rel)` contains `test_lazy_load`.

### C5. Optional general fix: cold per-test coverage for everything (map build only, not charged)
`xargs -P 8` over all 1,620 ids, running `coverage run --context=<id> -m pytest <id>`. That is about
1,620 × ~1.5 s / 8 ≈ 5-8 min, plus executor `setUpClass` (TPC CSV load) × 60. It removes first-test
attribution, `setUpClass` attribution and collection blindness in one go. When merging, keep the warm run's sets
for module-level lines, so that `file_cov` / `import_time_change` do not become all tests for every file. Add
the cold sets only for function-body lines. Do C2-C4 first; C5 is the thorough version.

### C6. `sqlglotc/` routing (cost)
In `python_change` before `"other-python"` (`selector.py:344`), and in `select` for non-.py files under `sqlglotc/`:
```python
        if rel.startswith("sqlglotc/"):
            return set(self.by_file.get("tests/test_sqlglotc.py", set())) or set(self.all), "sqlglotc"
```
Only `sqlglotc/setup.py` is executed by tests (`test_sqlglotc.py:25-27`). `sqlglotc/pyproject.toml` and
`MANIFEST.in` are read by nothing in `tests/` (grep). This rule comes from the pinned source, not from answers.

## (d) Ranked recommendations

1. **C1 fn-import-only fallback** (selector, 6 lines). Fixes a guaranteed empty selection for edits to
   `AutoName._generate_next_value_`, `_build_gen_dispatch`, `_build_coerces_to` and parts of `subclasses`.
   Each case saves 300 s, and it costs nothing on patches that don't touch such code.
2. **C2 import flag + setUpClass broadcast** (map rebuild, no new test run). Catches `Expression.__init_subclass__`
   (today 2 tests), dialect classes created at collection, and whole-class `setUpClass` failures (optimizer 21
   and executor 3 tests uncovered today).
3. **C3 order-perturbation run**. The only data-driven way to see per-process caches (`_build_dispatch` 67 tests,
   `_Dialect.__new__` 65, `_Tokenizer.__init_subclass__` 65, `_try_load` 75) that break hundreds of tests. Costs
   one extra coverage run at map-build time and slightly larger selections for edits to those functions.
4. **C4 cold runs for `test_lazy_load`, `test_dialect_imports`, `test_dialect_entry_points`**. Closes the
   subprocess and import-lock blind spots: any eager dialect import added at module level in the
   `import sqlglot` closure.
5. **C6 `sqlglotc/` routing**. Pure cost win: about 100 s → 3 tests on such patches. No recall risk, because
   nothing else executes `sqlglotc/`.
6. **C5 full cold per-test map**. The most thorough option; do it if time allows after 1-4.

Note: nothing here was run. Before relying on C1-C4, check the counts quoted above against the rebuilt map.
For example, count the lines in `files[*]["import"]` and `["once"]`, and confirm that `test_lazy_load` now
appears in `file_cov("sqlglot/schema.py")`.
