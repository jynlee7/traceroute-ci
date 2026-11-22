# Plan: lowest possible score on the blast-radius test-selection task

Goal: for each sqlglot patch, print the smallest, best-split set of test numbers that still contains every failing test, and spend almost no CPU doing it.

## 1. The scoring math and what it means for us

Per commit: `cost = max worker load + 2 s × workers`, plus **300 s per miss**. Then the run's `total = Σ cost + CPU time of the container`. The score is the middle of 3 runs.

What follows from that:

| Lever | Size | Consequence |
| --- | --- | --- |
| A miss | 300 s | Dominates everything. Recall comes first. |
| Adding a test of `d` s to a k-worker split | about `d / k` | Cheap. Include a test when `P(fail) × 300 > d / k`. For a 0.03 s test that threshold is about 1e-5, so if a test is plausible at all, include it. |
| Each extra worker | 2 s | Pick k per commit. With total load `L`, the cost `L/k + 2k` is lowest at `k ≈ √(L/2)`. For example, L=8 s gives k=2 and L=128 s gives k=8. |
| Running nothing (`[]`) | 0 s | The biggest per-commit saving, but only safe when `P(any fail) < selection cost / 300`. For a selection costing about 5 s, that means under ~2%. |
| Running pytest at decision time | CPU ≈ the tests' duration | Usually **worse** than listing those tests: listing S costs about `ΣS/k`, while running S locally costs about `ΣS`. Use only cheap checks such as `py_compile` or AST diffs. |
| Our own CPU | ms per process | Keep the runtime to one light process with precomputed maps and no imports of sqlglot. |

Strategy in one line: **high-recall selection from a precomputed map, an exact choice of k with bin packing, `[]` only for provably inert changes, and near-zero runtime CPU.**

## 2. Phase 0: setup and baseline (≈1 h)

1. Unpack the kit into the repo root (it has no top-level folder) and commit it. Keep the tarball out of the repo.
2. Run `make sample` with the starter kit (it runs every test) and record the baseline score.
3. Make the GitHub repo **public** before submitting. It is private now, and the task requires a public repo.
4. Write `analyze.py` to profile the sample data:
   - The test count, the total and per-file durations, and the slowest tests (the slowest one sets a floor on max load).
   - For each sample commit: which files it touches, its `kind` from `answers.json`, how many tests fail and in which test files, and how many commits fail nothing.
   - How often failures land outside the "obvious" test file, such as `test_<dialect>.py` for a dialect change.

## 3. Phase 1: build the test-impact map offline (≈half a day)

All of this runs on our machine. We commit the output, so the Docker build stays fast. The map must fit in the 20 MB repo limit.

1. Clone sqlglot at `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` and install it as the requirements pin it.
2. Collect **per-test coverage**: `pytest --cov=sqlglot --cov-context=test`, then export `line → {test numbers}` for every sqlglot source line.
3. Fix coverage's blind spots:
   - **Import-time lines** (module-level code, `def`/`class` lines, decorators, class attributes, dialect registration through metaclasses) are recorded only for the first test, or under an empty context. Map them to "every test that imports or executes anything in this module/class". Sources for that are the static import graph and the class → tests set built from method coverage.
   - **Added lines** have no coverage. Map them to the coverage of the enclosing function or class, plus the neighbouring old lines in the hunk.
   - **Deleted functions** map to the union of coverage of their body.
4. Map test files to numbers: a change to `tests/foo.py` selects every test in `tests/foo.py`, and every test in any test file that imports it, such as shared helpers.
5. Build a **static fallback map**, file → test files: `sqlglot/dialects/x.py` → `tests/dialects/test_x.py`, plus every dialect test that transpiles to or from x. The tables in `tests/dialects/*` use `write={...}` and `read={...}` across dialects, so grep them for dialect names.
6. Store compact tables: a `file → line → bitset/list of test numbers` table and a `file → function ranges` table, from the AST of the pinned source. Use a compact binary or packed JSON format that loads in milliseconds.

## 4. Phase 2: calibrate on real failures, not just coverage (≈1 day, the highest value)

Coverage says which tests *execute* a line, not which *fail* when it changes. The 30 sample commits are too few to tune on. Make more ground truth:

1. **Mutation corpus.** Write a generator that makes patches like the hidden ones. Mimic the sample `kind`s and diff styles: operator swaps, changed constants and strings, removed branches, renamed tokens, comment and docstring edits, and edits to test files. Run the **full** suite on each mutant, locally with `pytest -n auto` and the same "fail = failure, error, timeout or import error" rule. Aim for hundreds to low thousands of mutants.
2. From the corpus, measure the **recall of each selection rule**: coverage only, coverage plus function expansion, plus the dialect fallback, and so on. Find the classes of change where coverage misses failures. Expected ones include generator and tokenizer tables, `Expression` arg types, `TokenType` enums, dialect `FEATURE` flags, and anything consumed through `getattr` or dict lookups.
3. For each high-fanout area, decide: **widen** (add the whole relevant test files) or **run everything**. Base the choice on expected cost.
4. Learn a per-change risk score `P(any fail)` from simple features: kind, AST-changed or not, module-level or not, and coverage-set size. Use it **only** to decide `[]` vs. non-empty.

## 5. Phase 3: the "run nothing" decision

Output `[]` only when the change is shown to be inert, for example when all of these hold:

- The patch touches no `.py` file under `sqlglot/` or `tests/`, and nothing that tests read (fixtures, `.sql` files under `tests/fixtures`, `setup.cfg`/`pyproject` pytest config, `conftest.py`).
- Or the Python **AST is identical** before and after (comments and whitespace only) **and** no docstring changed (doctests) **and** the file still compiles and keeps its encoding and indentation.

The task says "a change that only adds a comment is not promised to be harmless." Treat these as risky:

- A `#` inside a string literal. That changes the string and is not a comment.
- A comment that breaks an indented block or a line continuation.
- An encoding cookie.
- A `# type:` or `# noqa` comment where tests check them.
- Any test that reads its own source or a fixture file.

The AST comparison catches the first two. Check the corpus for any case the AST check calls inert but some test fails, and tighten until there are none.

When unsure, select. One miss costs as much as ~60 commits of a 5 s selection.

## 6. Phase 4: splitting across workers (≈1 h, exact)

For each commit's chosen set:

1. Durations in ms are `round(seconds × 1000)`, the same way the scorer computes them.
2. For each `k = 1..8`, use LPT bin packing: sort the tests by duration, largest first, and give each to the least-loaded worker. Then improve with a few passes of pairwise swaps or a small DP when the set is small. Compute `max load + 2000 × k` exactly.
3. Pick the k with the lowest cost. A tie goes to fewer workers.
4. If one test exceeds `L/k`, it is the floor. Fewer workers then win.

This step is deterministic, cheap, and can be tested in isolation against `score.py`.

## 7. Phase 5: runtime and image (≈2 h)

- **Build:** a slim base image. Copy in the committed maps and `selector`. **Do not run sqlglot tests in the build.** Clone the pinned sqlglot only if the runtime AST check needs the original source. Better: commit the pinned source files we need, or precomputed per-file AST hashes and line → function tables, so the build makes no network call.
- **Runtime:** one process. Read `tests.json` and the patches, look up the maps, print the lines. Don't import sqlglot. Avoid subprocesses. Target a total CPU time well under 1 s.
  - Use Python with `-S -E` and few imports (`json`, `ast` only when needed).
  - If profiling shows interpreter startup or the JSON load matters, switch the map to a precompiled `marshal`/`pickle` file, or move the hot path to a small Go/Rust binary.
- **Output hygiene:** exactly one line per commit and every commit covered. Output must be deterministic (sorted lists, no set ordering). Write nothing to stdout except the JSON lines, and send all logs to stderr. Write nothing outside `/tmp`, and keep `/tmp` writes under 64 MB.
- **Robustness:** for a patch we can't parse, or a file not in the map, fall back to "all tests of the matching test files", or to all tests. Never crash, because a crash or invalid output rejects the whole run.

## 8. Phase 6: validation loop

1. `make sample` after every change, and track the score in `RESULTS.md`, with simulated time, misses and CPU recorded separately.
2. **Do not tune to the 30 sample commits.** The final set is larger (about 1.5×) and different. Judge rules mainly on the mutation corpus, with the sample as a held-out sanity check. Any rule that only helps on sample commits is suspect.
3. A unit test that feeds synthetic outputs through `score.py` and the validity rules: duplicate tests, empty workers, more than 8 workers, unknown ids.
4. Time-box: stop optimizing a lever when its expected gain is smaller than the risk of one extra miss.

## 9. Priority order, if time is short

1. Phase 4, optimal k and packing: an easy, guaranteed win over the starter kit.
2. Static file → test-file map, with whole test files selected: a big cut with low risk.
3. `[]` for AST-inert, non-test, non-fixture changes.
4. Per-test coverage map with function and class expansion.
5. The mutation corpus, to calibrate recall and widen the risky areas.
6. Runtime CPU micro-optimizations.

## Open questions to answer from the sample data in Phase 0

- What `kind` values exist, and how many sample commits fail nothing?
- What are the total suite duration and the slowest single test? These set the k range and the worst-case cost.
- Do failures ever spread across many dialect test files from one change? That decides how wide the dialect fallback should be.

## Progress log

### 2026-11-20: first working selector (measured on the sample, host run)
- Per-test coverage map built from the pinned commit (`tools/coverage.sh`, `tools/build_map.py`), stored in `maps/impact.json.gz` (~770 KB).
- `test_optimizer` and the TPC executor tests do their work in spawned process pools, which per-test coverage misses. Fixes:
  - `test_optimizer` was re-run with a thread pool (`tools/coverage_optimizer.sh`).
  - The pool's pickling path (`Expression.__reduce__` → `sqlglot/serde.py`) is mapped to every pool test.
  - The TPC executor tests get proxy coverage from the in-process TPC tests plus all executor and planner code.
- Sample: **192.3 s simulated, 0 misses**, against ~1,200 s for the starter kit.

### 2026-11-21: research-driven rules (code only, not yet run)
- `tests/test_docs.py` uses unittest `load_tests`, which pytest ignores, so docstring edits break nothing through doctests.
- `tests/test_typing.py` reads every `sqlglot/typing/*.py` file directly → any typing change selects it.
- Fixture loaders in `tests/helpers.py` drop empty lines and lines starting with `--` → `.sql` fixture edits limited to such lines select nothing.
- A dialect is split across `sqlglot/{dialects,parsers,generators,typing,tokenizers}/<name>.py`. Class-body and module-level edits now take:
  - coverage of the whole dialect family, including subclass dialects found through imports;
  - the dialect's test file, plus tests that hold the dialect name as a string (`dialect="x"`, `write={"x": ...}`);
  - coverage of every line naming the edited top-level class or its subclasses, and their `<lower>_sql` generator methods.
- Lambdas in class tables (parser `FUNCTIONS`, etc.) use the line coverage of the lambda itself.
- New or renamed methods select every caller of that name, because of overrides and generator lookup by name.
- A module-level edit in a package `__init__.py` runs everything.
- `changed_statement_lines` no longer widens an edit to a `def`/`class`/`if` header, which over-selected for small classes.
- Coverage sets are built lazily to cut charged runtime CPU.
- The static index (`tools/build_index.py`) is built in `docker build`, where CPU is not charged. If it fails, the build still succeeds with an empty index, and the selector falls back to file coverage.

**Next:** run `tools/eval.sh` and `make sample` to measure these changes, then commit and make the repo public.

### 2026-11-21 (later): 20-agent research merged (code only, not yet run)
All 20 reports are in `research/`. Merged into `selector.py`, `tools/build_index.py`, `tools/build_map.py`, `tools/fetch_src.py` and `Dockerfile`:
- **Recall:**
  - lambda-adjacent table insertions; import-time, class-creation and run-once functions; functions that ran only during import;
  - test-module import graph (Validator), sqlglot names imported by tests, removed names imported internally;
  - tests that loop over every dialect, dialects named in fixture headers, `"x, k=v"` dialect strings;
  - SQL-name map for auto-registered Func classes; renames, binary files, unparseable patches; byte-level comment hazards.
- **Cost:**
  - LPT plus improvement split; docstring-only edits select nothing; `sqlglotc/` routing;
  - slow-test pruning, limited to same-indent replacements on recorded lines; TPC-H proxy dropped; memoisation.
- **Build:** no apt or git (GitHub archive fetch); the index build can't fail the image; a bad map runs everything.

**Next:** rebuild the map → `tools/eval.sh` → `make sample` → fix → commit → make the repo public. Deferred: agent 13 bake (CPU), agent 01 parser-table narrowing, agent 14 coverage re-runs (import flag, setUpClass broadcast, reversed-order run).
