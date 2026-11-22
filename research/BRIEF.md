# Research brief (read this first)

Repo: /Users/leej741/projects/traceroute-ci. This is a competition entry: for each patch to sqlglot (pinned at eb11aec4be8cb2122afe713a4816662c3f0fb2fa), print which tests to run and how to split them across ≤8 workers.

Read: `README.md`, `SCORING.md`, `plan.md` (the progress log at the end shows the current state), `selector.py` (the current submission), `tools/build_map.py`, `tools/build_index.py`, and `Dockerfile`.

Scoring: per commit, `max worker load + 2 s × workers`, plus **300 s per missed failing test set**, plus the container's CPU time. A miss is the worst outcome. After that, the slow tests (`test_parser::test_max_nodes` 28.8 s, `test_executor::test_execute_tpcds` 21.5 s, `test_generator::test_generator_memory` 6.7 s) set the cost floor whenever they are selected.

Data:
- `sample/tests.json` has 1620 tests and 103 s total. `sample/commits/*.patch` holds 30 sample patches, and `sample/answers.json` their failing tests and kind (code, fixture, comment, multi).
- `work/sqlglot/` is a checkout of sqlglot at the pin, with its source and tests.
- `maps/impact.json.gz` is the coverage map: `{tests:[ids], sets:[[test idx]], files:{rel:{lines:{lineno:set_id}, ranges:[[start,end,kind]]}}, test_strings:{idx:[strings]}, test_ranges}`.

Current status: the selector scores 192.3 s simulated with 0 misses on the sample (version from 2026-11-20). The rules added on 2026-11-21 have not been run.

## Hard rules for every agent
1. **Do not run** `selector.py`, `score.py`, `tools/*.sh`, `tools/eval.sh`, `make`, `docker`, `pytest`, or sqlglot's code. No local testing of any kind.
2. You may read any file, use grep/find/sed, and run small read-only Python one-liners that only *inspect* data files (json/gz) under `sample/` or `maps/`.
3. **Write only your own output file** `research/<NN>-<slug>.md`. Do not edit `selector.py` or any other file. No git commands.
4. Competition rule: there must be no table that maps a patch, or a kind of edit, to the tests it breaks. Maps derived from the pinned commit (coverage, imports, fixtures, static analysis) are allowed. Don't propose anything that tunes to the 30 sample answers.
5. In your output file, give:
   - (a) findings, each with file:line evidence;
   - (b) concrete recall risks (patch shapes that would cause a miss under the current `selector.py`) and cost waste;
   - (c) proposed code changes as exact Python snippets or unified diffs against `selector.py`, `tools/build_index.py` or `tools/build_map.py`, with where they go;
   - (d) a ranked list of recommendations with expected impact.

   Keep it under ~250 lines and be concrete. When you finish, reply with a 5-line summary.
