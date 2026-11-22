# Round-2 research brief: get the score under 100 s (read this first)

Repo: /Users/leej741/projects/traceroute-ci. A competition entry. For each patch to sqlglot (pinned at eb11aec4be8cb2122afe713a4816662c3f0fb2fa), it prints which tests to run and how to split them across ≤8 workers.
Read: `README.md`, `SCORING.md`, `plan.md` (progress log), `selector.py`, `tools/build_map.py`, `tools/build_index.py`, `Dockerfile`. Round-1 reports are in `research/*.md`, so don't repeat them; build on them.
Per-commit reasons from the last host run are in `work/why.log`, and that run's output is in `work/out.jsonl` and `output.jsonl`. The pinned sqlglot source and tests are in `work/sqlglot/`. The coverage map is `maps/impact.json.gz`.

## Where the score stands (last `make sample`, 30 commits): 217.7 s simulated, 0 misses
Per commit: cost / failing tests / selected / the biggest selected tests
```
2haz code    fail 0  sel 101 k2 25.5  tpcds 21.5, test_scope 2.5
4d63 code    fail 0  sel 561 k3 27.5  tpcds 21.5, generator_memory 6.7
66ks code    fail 0  sel  51 k1 24.8  tpcds 21.5
2jrq code    fail 8  sel  32 k2 25.5  tpcds 21.5   <- tpcds really fails
3utz code    fail 37 sel 181 k2 27.3  tpcds 21.5   <- tpcds + tpch really fail
535z code    fail 1  sel 171 k2 11.2  generator_memory 6.7
fryt code    fail 0  sel  37 k1  9.2  generator_memory 6.7
gbav code    fail 0  sel  42 k1 10.1  generator_memory 6.7
ktz3 code    fail 0  sel 134 k2  7.1
2qn5/5y72/8y7j code fail 0 cost 2.0-2.5   (should be [] = 0)
rszg 5.8, ary6 4.4, j6uz 4.4, bj7g 3.6, fixtures ~2-3 each, 3 comment commits 0
```
12 of the 30 sample commits fail nothing. Kinds: code 16, fixture 8, multi 3, comment 3.
Slow tests: test_max_nodes 28.8 s, test_execute_tpcds 21.5 s, test_generator_memory 6.7 s, test_execute_tpch 3.9 s, test_optimized_tpch 3.5 s, test_scope 2.5 s, optimizer test_tpcds 2.2 s, test_annotate_funcs 2.0 s.
**Rough floor on the sample is ~85 s:** 2jrq and 3utz really do fail tpcds, and every commit with a failing test costs at least 2 s. Getting under 100 s therefore needs: (1) `[]` for the commits that break nothing, (2) slow tests selected only when they can really fail, and (3) tight selections elsewhere. All of that has to hold at **zero misses**, because one miss costs 300 s. The hidden final set is about 1.5× larger and different, so every rule must be a *general principle* derived from the pinned source, coverage, or static analysis, never fit to these 30 answers.

## Hard rules for every agent
1. **NO LOCAL TESTING.** Do not run `selector.py`, `score.py`, `tools/*`, `make`, `docker`, `pytest`, or any sqlglot code, and don't import sqlglot.
2. You may read files, use grep/find/sed, and run small read-only Python one-liners that only *inspect* data (json/gz/patch/.py source text, the `ast` module on source text) under `sample/`, `maps/`, `work/`.
3. **Write only your own output file** `research/r2/<NN>-<slug>.md`. Edit no other file. No git commands.
4. Competition rule: no table mapping a patch, or a kind of edit, to the tests it breaks. Maps derived from the pinned commit are allowed. Don't tune to the 30 sample answers. Using them to *sanity-check* a general rule is fine.
5. Your output (≤ ~250 lines) must include:
   - (a) findings with file:line evidence;
   - (b) estimated savings in seconds on the sample, and the recall risk on hidden commits, with patch shapes that would cause a miss;
   - (c) exact code changes as Python snippets or unified diffs against `selector.py` / `tools/build_index.py` / `tools/build_map.py`, with where they go;
   - (d) a ranked list of recommendations, each with expected s saved and a risk rating.
   Finish by replying with a 5-line summary.
