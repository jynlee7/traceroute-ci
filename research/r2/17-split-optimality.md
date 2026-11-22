# 17 - Is the worker split optimal? And is padding below the dominant test free?

Method: read-only. I read `selector.py:830-915` and ran a Python `-I` one-liner over `output.jsonl` and `sample/tests.json`. It uses pure arithmetic and does not run the selector or any sqlglot code.

## (a) Findings

1. **The split is exactly optimal on all 30 sample commits.**
   - I recomputed the pigeonhole lower bound from the code (`_lb_ms`, selector.py:836-841): max(largest test, ceil(total/k), d[k-1]+d[k]).
   - I took the minimum of `LB(k) + 2000k` over k=1..min(8,n), using the test set actually printed in `output.jsonl`.
   - The emitted cost equals that bound on every non-empty commit, so the gap is **0 ms** each time. The bound is a true lower bound, so no split of the same set can do better.
   - The totals match: the sum of emitted costs is 217677 ms, and the sum of the bounds is 217677 ms. That matches the 217.7 s on the brief.
   - Examples:
     - 2haz: k=2, 25454 = 21454 + 4000.
     - 3utz: k=2, loads 23321 / 23320, which is optimal to the ms.
     - 4d63: k=3, loads 21454 / 21232 / 21231, which is the pigeonhole bound.
     - 535z and ktz3: perfectly balanced.
   - Nothing remains to gain in the split itself. The score is decided by selection.

2. **The cost model in the code matches SCORING.md** (lines 36-42): cost = max load + 2000 per non-empty worker.
   - `WORKER_MS = 2000` is at selector.py:27.
   - `lbs[k]` adds `WORKER_MS * k` (selector.py:906).
   - Empty bins are dropped by `if b` (selector.py:913).
   - Empty selection returns `[]` at selector.py:899.

3. **Rounding.** `ms = round(t["seconds"]*1000)` (selector.py:921) is the same formula SCORING.md gives.
   - In `sample/tests.json` (1620 tests) no test has `seconds*1000` within 1e-6 of a .5 fraction. Python's half-to-even rounding and the scorer's rounding therefore cannot differ here.
   - Hidden-set risk is limited to a hidden tests.json with an exact x.5 ms value. If the scorer used half-up and the selector half-even, a worker would be off by 1 ms.
   - Cost: 1 ms at worst. Fix: `int(t["seconds"]*1000 + 0.5)`. Ties are not an issue in the sample.

4. **Tie-breaking** is deterministic.
   - Orders are `(-ms, n)`.
   - k candidates are `(lbs[k], k)`, so fewer workers win a tie.
   - The best update uses `(cost, k) < (best[0], best[2])`.
   - The search is also correct about stopping: `best[0] <= lbs[k]` breaks only when the sorted lower bound cannot beat the best.
   - `_improve` is a heuristic with a 64-move cap (selector.py:851). It happens to reach the bound on all 30 commits. In hidden commits with many near-equal small tests it can stall above the bound by a few ms. That is negligible next to the 2 s per worker.

5. **k=1 when one test dominates.**
   - 66ks: k=1, load 22787 (tpcds 21454 + 50 others), cost 24787.
   - Splitting to k=2 would give max 21454 and cost 25454, which is 667 ms worse. So k=1 is correct.
   - Rule: a second worker pays off only if it removes more than 2000 ms from the max. This holds when the non-dominant load exceeds 2000 ms (2haz, 2jrq, 3utz, 4d63).

6. **Slack below the dominant test, per sample commit** (slack = top load - worker load, per non-top worker):

   | commit | loads (ms) | free slack |
   |---|---|---|
   | 2haz | 21454 / 11474 | **9980 ms** |
   | 2jrq | 21454 / 7882 | **13572 ms** |
   | 3utz | 23321 / 23320 | 1 ms |
   | 4d63 | 21454 / 21232 / 21231 | 222 ms and 223 ms |
   | 535z, ktz3 | balanced | 0 |
   | 66ks | k=1 (22787) | **0 at k=1**; k=2 costs +667 ms and then gives about 20.1 s of slack |

   - Only 2haz and 2jrq have real free slack on the sample: **23.5 s of test time in total**.
   - Padding is free only in the narrow sense that max load stays unchanged and k does not change. It must never push any load above the current max, and it must never add a worker.
   - The 66ks case is not free: padding means k=2, which costs +667 ms. It is cheap, but only worth it if the padding buys real recall.
   - Check on the "free" claim: the dominant test is 1106 (tpcds, 21454 ms). The k=2 worker that holds the rest has load L2 < 21454. Adding tests with total <= 21454 - L2 leaves cost = 21454 + 4000 exactly.

7. **Padding is not a general win, so be careful.** Slack exists only when a dominant test is selected and the remainder is under that test's load.
   - Of 30 sample commits, the dominant tests are selected in 5 (2haz, 4d63, 66ks, 2jrq, 3utz), and only 2 have slack.
   - Every other commit is cheaper than 11 s, where each added test adds cost one-for-one.
   - So padding insures recall only on the commits where the 21.5 s tpcds is selected. 2jrq and 3utz really fail tpcds. 2haz, 4d63 and 66ks select it but nothing fails, so on those the right fix is to stop selecting tpcds (another agent's topic; that gives 4d63 about -21 s, 66ks about -22 s and 2haz about -23 s).
   - **Interaction:** every time a tpcds-dropping rule removes the dominant test, the slack disappears. Padding then costs real seconds. Padding should therefore be computed after the final selection, from the realized split, never assumed.

8. **Failure modes of padding.**
   - If the dominant test is removed, no slack exists and padding adds nothing, which is fine because the algorithm is conditional on slack.
   - It raises no risk of a miss.
   - It does not reduce cost and does not fix a miss for a test that was never considered.
   - Its recall value depends on candidate ranking. Padding with random tests recovers about slack / 103 s of the whole suite, which is nearly nothing (9.98 s / 103 s is about 10% of tests by time, selected arbitrarily).
   - Padding only helps if candidates are ranked by "plausibility", for example tests covering the touched files at any line, then the same dialect family, then the same module.

## (b) Savings and risk
- Split change: **0 s** saved. The split is optimal.
- Rounding change: 0 s on the sample, a tiny hidden-set safeguard.
- Padding: **0 s direct saving** by design. It is recall insurance on commits where the dominant test is kept. It is worth about 2 commits out of 30 on the sample (2haz, 2jrq).
- Recall risk of padding: none (adds tests only). Cost risk: an off-by-one bug could push the max above the dominant test, so the code must guard with a hard cap on `max(loads)` and `len(workers)`.
- Patch shapes where padding helps: a commit touching generator or parser code where tpcds is selected for coverage reasons and a related dialect test was missed by the coverage map (e.g., a test that goes through the changed file via `parse_one` but never touches the changed lines).

## (c) Code: padding after the split (selector.py `main`, after `workers = split(chosen, ms)`)
```python
def pad_free(workers, ms, candidates):
    """Fill spare capacity on non-top workers with extra plausible tests, never raising max load or k."""
    if len(workers) < 2:
        return workers   # k=1: any addition costs its full ms
    loads = [sum(ms[n] for n in w) for w in workers]
    cap = max(loads)
    have = {n for w in workers for n in w}
    for n in candidates:               # ordered most plausible first
        if n in have:
            continue
        # first worker with room (best fit: the fullest worker that still fits)
        fits = [i for i in range(len(workers)) if loads[i] + ms[n] <= cap]
        if not fits:
            continue
        i = max(fits, key=lambda j: loads[j])
        workers[i].append(n)
        loads[i] += ms[n]
        have.add(n)
    return [sorted(w) for w in workers]
```
and in `main`:
```python
workers = split(chosen, ms)
workers = pad_free(workers, ms, sel.secondary(files, chosen))   # sel.secondary: new, see below
```
`Selector.secondary(files, chosen)` is a new method. It would return tests, ordered by plausibility, taken from `self.file_cov(rel)` (selector.py:244, tests that run any line in a touched sqlglot file), then `dialect_family`/`name_tests` (selector.py:262, 281), excluding `chosen`. Sort key: (rank tier, ms). The dominant test must never be used as candidate fill.
Guard to add: assert `max(loads)` and the number of non-empty workers are unchanged after padding, and fall back to the unpadded `workers` if not.
Rounding fix at selector.py:921: `ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) ...}` only if SCORING.md is confirmed half-up; the Python 3 `round()` is half-even, and there are no ties in the sample.

## (d) Ranked recommendations
| # | Recommendation | Expected saving | Risk |
|---|---|---|---|
| 1 | Do not touch `split()`. It is exactly optimal (gap 0 ms on 30/30). Spend effort on selection (drop tpcds when it cannot fail). | 0 s, but avoids wasted work | none |
| 2 | Add `pad_free` as a post-split step with strict caps (max load and k unchanged). Only affects commits that keep the dominant test. | 0 s; recall +up to 10-13 s of tests on 2haz and 2jrq | low (cost guard); the value is limited by candidate ranking |
| 3 | Do the padding **after** all selection rules, never before; remove tpcds first, then pad only if slack remains. | avoids phantom cost | low |
| 4 | Make the rounding match the scorer explicitly (half-up) | 0 s sample; 1 ms hidden worst case | negligible |
| 5 | For 66ks-like k=1 dominant commits, do NOT split just to pad: +667 ms for slack. Only worth it if the padded tests have real failure probability. | -0.7 s if done blindly | low |

## 5-line summary
1. Split is optimal on all 30 commits (total 217677 ms, equal to the pigeonhole lower bound; gap 0 ms), k choice and tie-breaks are correct.
2. Rounding matches SCORING.md. The sample has no x.5 ms ties, and the cost model is right.
3. Free slack exists only where a dominant test is selected with k>=2: 2haz 9.98 s, 2jrq 13.57 s; 3utz and 4d63 about 0; 66ks has none at k=1.
4. "Free recall padding" is valid but small: a post-split `pad_free` with a hard cap on max load and k, filled from ranked secondary candidates; do it only after tpcds-dropping rules, since removing tpcds deletes the slack.
5. No seconds saved by the split; the gains are all in selection. Code is in section (c).
