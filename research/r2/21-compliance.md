# 21 - Compliance audit of the "no answers worked out ahead of time" rule

Rule: no table from a patch, or a kind of edit, to the tests it breaks. Maps built from the pinned commit (coverage, imports, fixtures) are allowed. Top entries are read.
Nothing was run. Everything below comes from reading `selector.py`, `tools/build_index.py` and `tools/build_map.py`.

## (a) Audit findings

Short version: no kind-of-edit -> tests table exists. There are five items to tidy or document.

1. **No hard-coded test IDs anywhere in `selector.py` or `tools/build_index.py`.**
   - `grep` for `::test_` and `tests/...py` finds only these:
     - `selector.py:25`, `TYPING_TEST = "tests/test_typing.py"`, a file path (see item 4).
     - `selector.py:660-662`, `tests/test_sqlglotc.py` for `sqlglotc/` edits (see item 4).
   - `all_dialect_tests` (`build_index.py:304-327`) is derived by AST (`loops_all`). Nothing is named by hand. This is clean.
2. **`tools/build_map.py:120-122` and `:139-145` name tests by ID.**
   - They name `tests/test_optimizer.py::*`, `test_execute_tpch`, `test_execute_tpcds`, `test_optimized_tpch`, `TestOptimizer::test_tpch` and `TestOptimizer::test_tpcds`.
   - The purpose is to repair coverage the tracer cannot see:
     - the spawned process pools lose coverage;
     - `serde`/`__reduce__` pickle lines and `executor/*`, `planner.py` and `generators/python.py` are credited to the tests that really run them in children.
   - This is a map built from the pinned commit that fixes a measurement gap. It is a map from code lines to tests, which is allowed, and it does not depend on any patch.
   - It is the closest thing in the repo to a special case for `test_optimizer`. A reviewer could read "name tests, then credit them with lines" as a hand-written table.
   - **Risk: low-moderate.** The fix is to derive it (see (c)).
3. **`selector.py:31` and `:512-519`, the slow-test prune.**
   - Mechanics:
     - `SLOW_MS` is computed from `tests.json` durations, which are pin data.
     - The rule applies only when the diff is a same-indent one-for-one line replacement.
     - It then narrows from the +-2 line window to the exact line's coverage.
   - Compliance:
     - The narrowing uses the coverage map. The output is still "tests that executed this line".
     - It is gated on the *shape* of an edit, but the shape only widens or narrows the coverage query. It never names tests or outcomes.
     - This is not a kind-of-edit -> tests table. It is grey only if someone phrases it as "for edits of kind X, drop slow tests".
     - It is also a recall risk, because it assumes a line replacement cannot alter behaviour through a neighbouring line. Compliance and recall are separate questions, and this one is compliant.
4. **Path-to-test special cases at `selector.py:25` and `:660`.**
   - They say "sqlglotc/ is only run by test_sqlglotc.py" and "tests/test_typing.py reads typing/*.py directly".
   - Both are import and read relationships from the pinned tree: file reads or `setup.py` loads. Both are map-like.
   - Better practice is to derive them: grep the test sources for `sqlglotc` and `typing` path strings. `test_strings` in the map already does something similar.
5. **Comment, docs and fixture shortcuts (`select`, `selector.py:640-680`).**
   - These are classes of edit (comment-only, docs suffix, `--` fixture lines) that map to "no tests". This is the most table-like part of the file.
   - It is defensible because it is semantic: a comment cannot change what executes. It is not a lookup of observed failures.
   - Keep the wording as "edit is a no-op for the interpreter / loaders", never "edits of this kind never fail".
   - Verify the loader claim against `tests/helpers.py` at the pin (`selector.py:631` already does).

Overall verdict: **compliant as written.** The residual exposure is item 2, plus any new rule that stores a probability, a count or a test name derived from the 30 sample answers.

## Bright lines

A rule is allowed when all of these hold:
- (i) its inputs are the patch text, the pinned tree (source, tests, fixtures), `tests.json` (durations, IDs), and tracer or static-analysis output computed from the pin;
- (ii) its output is "tests whose recorded behaviour on the pin touches X", where X is derived from the patch by a mechanical rule such as a line range, symbol or file;
- (iii) it contains no constant that was set by looking at sample answers, and every parameter (`WINDOW`, `SLOW_MS`) is justified by the pin's structure.

It is **not allowed** when any of these hold:
- a table keyed by edit kind (`if edit is X: tests = [...]`) where the table values came from watching what fails;
- a list of test names or files to exclude or include, written by hand because "they never fail" or "they are slow";
- a probability, rate or cost multiplier fitted on the sample commits;
- a rule you could only have written after reading `sample/answers`.

The test to apply: "Could this rule be regenerated from the pinned commit alone, with the patch as the only runtime input?" If yes, it is a map. If it needs knowledge of what failed, it is an answer table.

## Round-2 ideas: allowed, grey, or not

| Idea | Verdict | Why |
| --- | --- | --- |
| Slow-test gating by source dependency | **Clearly allowed** when it is "include slow test T only if coverage or the import graph says T executes the changed line or symbol". **Grey** when it is "slow tests are dropped unless X", with X a kind of edit. | The first is exactly a coverage map. The second is a pruning list keyed on edit kind. |
| "Observation channel" indexes | **Allowed** if built from the pin. **Grey** if the channel list is hand-picked. | See below. |
| Expected-cost model with probabilities | **Grey to forbidden**, depending on where the numbers come from. | See below. |
| `[]` for uncovered code | **Allowed** only where the map proves non-execution. Otherwise **grey**. | See below. |

### Slow-test gating by source dependency
- **Allowed** (this is what the repo does): a slow test is selected iff the coverage map ties it to the touched line, or to the symbol or module through the import-time rule.
- **Allowed**: ordering and splitting by `seconds` from `tests.json`. Durations are pin data.
- **Grey**: a gate keyed on the shape of the edit (`same_indent_replacements`) with a threshold.
  - Phrase it as "narrow the coverage query from the +-WINDOW lines to the exact line when the diff does not change the line structure".
  - Do not phrase it as "drop slow tests for simple edits".
  - Keep the threshold derived: for example, "slow" = duration above some quantile of `tests.json`, not the literal 5000 tuned on tpcds.
- **Not allowed**: "skip test_max_nodes / tpcds unless file X", written by name.
  - Implement it as "`pool_tests` and `coverage-blind` flags per test, computed by the build".
  - A coverage-blind test is one whose tracer-visible line count is small relative to its duration. It is selected on file-level import dependence instead.

### "Observation channel" indexes
An observation channel is a way a test can see a change: stdout, exceptions, generated SQL, repr or pickle, fixture reads, etc.
- **Allowed**: per-file or per-symbol channel indexes computed by static analysis of the pin. Examples:
  - "tests that call `Expression.__reduce__`";
  - "tests that read `tests/fixtures/optimizer/*.sql`";
  - "tests that load every dialect".
- All of these are import, call and read maps, like `all_dialect_tests`.
- **Grey**: a channel that needs the patch's *semantic kind* to choose it. Example: "a docstring edit has only the `__doc__` channel, so only tests touching `__doc__`".
  - Implement it by computing the channel set from the AST diff: the changed node is a docstring, so it is reached by a mechanical rule (`ast.get_docstring`). Then intersect with the index of tests that read docstrings or `__doc__`.
  - Record the evidence in the index, not in the selector.
- **Not allowed**: channels whose membership was decided from sample failures (for example "tests that tend to fail on typing edits").

### Expected-cost model with probabilities
- **Allowed**: a cost model whose inputs are pin-only, such as durations and coverage cardinalities. Use the model to *order or split* tests, never to *drop* a test that a map says is reachable.
- **Allowed**: deterministic structural scoring, for example "tests ranked by number of covered lines intersecting the diff".
- **Forbidden**: P(fail | edit kind) estimated from the 30 sample answers. That is a table from kind-of-edit to failures with extra steps.
- **Grey**: probabilities fitted on a mutation run of the pinned tree (mutate every function, record failures).
  - It is pin-derived and general, but it is "worked out ahead of time" in a way a reviewer could read as precomputed failures by edit.
  - If used, publish the generator in `tools/`, rebuild it in the Docker build from the pin, and never ship hand-edited numbers.
  - The mutation data would become a per-line failure map. That is closer to a coverage map and probably acceptable, but it is the highest-risk idea here.
- **Zero-miss caveat**: with a 300 s miss penalty, an expected-cost drop of any test whose reach the map confirms needs P(fail) below about 1% per 3 s saved. A model that cannot justify that precisely should not drop reachable tests.

### `[]` for uncovered code
- **Allowed** when the claim is "no test executes, imports or reads this code".
  - It must be proved by the map: no line coverage, no import-time or class-body role, and not read as text by any test or fixture.
  - It must also cover code that coverage cannot see (process pools, subprocesses, `exec`). The `build_map.py:136-157` repairs are what make this safe.
- **Grey**: `[]` for code that runs but is "unlikely to change any assertion" (dead branches, logging, type annotations).
  - Implement as: the changed lines are in the set of AST nodes with no runtime effect (annotations, docstrings, comments, `if TYPE_CHECKING:` bodies), found by a mechanical walk. Keep the rule semantic, as the comment rule already is.
  - Do not use "code unlikely to matter".
- **Not allowed**: `[]` because the sample says the commit failed nothing.

## (b) Savings and risk
- Compliance changes themselves save 0 s. They remove a rule-violation risk (disqualification) and shift a few items from hand-named to derived.
- The slow-test gate was already in place. Making `SLOW_MS` a quantile instead of 5000 should be neutral on the sample.
- The only real recall risk is the "narrow to exact line" rule on same-indent replacements, which is compliant but not free. A patch shape that would cause a miss:
  - a one-line replacement that changes a value consumed only through a neighbouring line's execution in a *different* test, such as a constant in a dict used by tpcds;
  - a changed default argument whose call sites run in a coverage-blind child process, where the exact line has no recorded tests;
  - a replacement inside a multi-line expression (continuation) that the `ln in prunable` guard catches only if the diff shows it.

## (c) Code changes (suggested, not applied)

1. Derive the pool and coverage-blind tests in `tools/build_map.py` instead of naming them (replace lines 120-122 and 139-145):
```python
import ast
def blind_tests(root, ids):
    """Tests whose bodies use process pools (ProcessPoolExecutor / multiprocessing / execute with the pool)."""
    out = set()
    for i, tid in enumerate(ids):
        path = root / tid.split("::")[0]
        src = _src(path)                  # cached read of the test module
        fn = _find_test_def(src, tid)     # ast lookup by name
        text = ast.get_source_segment(src, fn) or ""
        if any(k in text for k in ("ProcessPool", "multiprocessing", "Pool(", "execute(")):
            out.add(i)
    return out
```
   - This needs the spawned-pool tests to be identifiable by AST. If the helper they call lives in `sqlglot/executor/`, follow one call level.
   - Proxies for the pool tests: choose the tests that cover the same fixtures. Derive them from `test_strings` overlap (tests that read `tpc-ds.sql` / `tpc-h.sql`) instead of by name.
2. Replace `selector.py:25` and `:660-662` with index lookups.
   - Add `"path_readers": {"sqlglotc": [...], "sqlglot/typing": [...]}` in `build_index.py`, computed from string literals in the tests (`tokenize`/`ast.Constant` containing those paths). The selector looks up `self.index["path_readers"]`.
3. `SLOW_MS` becomes a quantile in `Selector.__init__`:
```python
ds = sorted(round(t["seconds"]*1000) for t in tests)
self.slow_ms = max(1000, ds[int(len(ds)*0.999)])   # slowest 0.1% of tests; pin-derived, not tuned on answers
```
   - Do not change the value to hit a score. Check only that it lands near today's set. The slowest 0.1% is about 3 tests, roughly the 3 tests of 6 s or more listed in the brief.
4. Add a comment block above the comment-only, docs and `--` rules in `select`: "These rules are semantic (no runtime effect); they do not encode which tests ever failed." That is the answer if a reviewer reads the code.

## (d) Ranked recommendations

| # | Recommendation | Seconds saved | Risk (compliance and recall) |
| --- | --- | --- | --- |
| 1 | Keep every round-2 rule in the form "the map says T executes or reads X". Reject any rule that needs a P(fail) or a named test. | 0, but protects the whole score | Eliminates disqualification risk |
| 2 | Derive the coverage-blind tests (`build_map.py:120-145`) from AST and `test_strings` instead of by ID. | 0 | Low (compliance); low (recall) if the derived set equals today's |
| 3 | `[]` only for edits proven to execute no tracer-visible, import-time or text-read code (comments, annotations, docstrings, `TYPE_CHECKING`, uncovered and not-pool-visible code). | about 7-10 s on the sample (the 2.0-2.5 s commits and a few more) | Moderate (recall). Gate on "no line in the changed function ran, directly or through import or pool maps" |
| 4 | Slow-test gating by dependency (existing rule) with a pin-derived threshold, applied as narrowing of the coverage query rather than by edit kind. | already in place | Low (compliance); moderate (recall) |
| 5 | Observation-channel indexes built from the pinned tests (call, read and import maps). | depends on the idea | Low if static; grey if channel choice depends on the patch's meaning |
| 6 | Expected-cost model: use only to order and split. Never use it to drop reachable tests. | small | Forbidden if fit on sample answers; grey if mutation-derived |

## Phrasing guidance for README and code comments
- Use: "built from the pinned commit", "computed from coverage, imports and fixtures", "pure function of (patch, pin)".
- Avoid: "never fails", "usually fine", "slow tests we skip", "typical", "kind of edit".
- State in the README that sample answers were used only to sanity-check the general rules.

Summary
1. No kind-of-edit -> tests table was found in `selector.py`, `tools/build_index.py` or `tools/build_map.py`. No hard-coded failing-test lists exist.
2. The closest thing to one is `build_map.py:120-145`, which names optimizer and TPC tests to repair pool coverage. Derive it by AST and `test_strings`.
3. The slow-test prune (`selector.py:512`) is compliant as an exact-line coverage query. Derive `SLOW_MS` from the pin and do not describe it as a pruning list.
4. Allowed: slow-test gating by dependency, static observation channels, and `[]` when the maps prove non-execution. Forbidden: probabilities fitted on sample answers.
5. Grey: edit-shape gates, mutation-derived probabilities, and `[]` for "unlikely to matter" code. Make each a mechanical AST or coverage rule built from the pin.
