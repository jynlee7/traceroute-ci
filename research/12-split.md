# 12 - Worker split: optimal k, LPT gap, improved split()

No code was run. Durations come from read-only one-liners over `sample/tests.json`. Everything else is worked by hand.

## (a) Findings

**F1. The current split (selector.py:463-481).**
- It runs plain LPT for every k in 1..min(8, n).
- Cost is `max(loads) + 2000*k`. Ties keep the smaller k, because the comparison is strict `<` (selector.py:479).
- `WORKER_MS = 2000` and `MAX_WORKERS = 8` are at selector.py:26-27.
- LPT has no improvement step and no lower-bound stop. It always builds all 8 candidates.
- `split()` never affects recall. It only changes the cost term.

**F2. Analytic lower bound.**
- Let T be the total ms, L the longest test, and d1 >= d2 >= ... the sorted durations.
- All durations are integers (`round(seconds*1000)`), so `ceil(T/k)` is valid.
- For fixed k: `LB(k) = max(L, ceil(T/k), d_k + d_{k+1} if k < n) + 2000k`.
- The last term holds because k+1 items on k workers force two onto one worker. It is cheap and sometimes tighter.
- The best possible cost is `min_k LB(k)`, and `min_k cost(k) >= min_k LB(k)`.

**F3. Where the optimal k lies.**
- Ignore L first. `T/k + 2000k` is convex. Going from k to k+1 saves `T/(k(k+1))`, which beats 2000 iff `T > 2000*k*(k+1)`.

| step | T must exceed (ms) |
|---|---|
| 1 -> 2 | 4,000 |
| 2 -> 3 | 12,000 |
| 3 -> 4 | 24,000 |
| 4 -> 5 | 40,000 |
| 5 -> 6 | 60,000 |
| 6 -> 7 | 84,000 |
| 7 -> 8 | 112,000 |

- The whole suite is 103,047 ms, so k=8 is never optimal on this data. It would need T > 112 s.
- With a dominant test, cost for k >= T/L is `L + 2000k`, which only grows. So `k* <= ceil(T/L)`.
- Together: `k* <= min(8, ceil(T/L), the table k above)`.
- Full-suite check:
  - T=103047, L=28817 (test_max_nodes, number 1490, per the one-liner).
  - ceil(T/L) = 4, so k*=4.
  - `LB(4) = 28817 + 8000 = 36817`. `LB(3) = 34349 + 6000 = 40349`. `LB(5) = 28817 + 10000`.
  - LPT at k=4 reaches the bound exactly. The remaining 74,230 ms sit on 3 workers (avg 24,743 < 28,817) and consist of many small items.
  - LPT is optimal whenever a giant dominates and the rest is fine-grained.
- Typical sets that include one of the three giants (28.8 s, 21.5 s, 6.7 s) have a bound of `L + 2000k` with k small. LPT hits the bound there.

**F4. Where LPT is suboptimal.** It only loses when no single test dominates and there are a few medium items (about 10 or fewer). LPT's worst-case ratio is `4/3 - 1/(3k)`: 7/6 for k=2, 11/9 for k=3, 5/4 for k=4.

- Tight k=2 example (ms x1000, i.e. 3000 3000 2000 2000 2000, T=12000):
  - LPT: A={3,2,2}=7000, B={3,2}=5000. Cost at k=2 is 7000+4000 = **11000**.
  - Optimal: {3,3} | {2,2,2} = 6000, cost **10000** (-1000 ms, -9%).
  - k=3 gives LB `max(3000,4000)+6000 = 10000`, and the true k=3 optimum is 5000 (cost 11000).
  - So LPT's best is 11000 versus 10000 for the optimum.
- Tight k=3 example (5,5,4,4,3,3,3 in seconds, T=27000):
  - LPT loads are 11/8/8, cost 17000.
  - Optimal 9/9/9 ({5,4}{5,4}{3,3,3}), cost 15000 (-2000 ms, -12%).
- Real durations from `sample/tests.json`: test_executor::test_execute_tpch 3863, test_optimized_tpch 3537, test_optimizer::test_scope 2533, test_tpcds 2232, test_annotate_funcs 1963 (T=14128).
  - k=2: LPT gives A={3863,2232}=6095, B={3537,2533,1963}=8033, cost 12033.
  - A swap of 3537 for 2232 gives 7400/6728, cost 11400. This is optimal for k=2.
  - k=3: LPT gives 5500, cost 11500. This is the true k=3 optimum (checked by hand).
  - Current `split()` returns **11500**. The improved version returns **11400**. LB(3)=10765 is not reachable.
  - The gap is small (-100 ms, 0.9%). The wasted amount is at most about one medium test minus the mean.
- Expected size of the win:
  - Zero for any selection containing a giant.
  - Roughly 0 to 12% of the split cost for sets of 3 to 12 medium tests with T of 12 s to 60 s.
  - Most commits in this competition select a handful of tests, so the average gain is small, on the order of 0 to 1 s per affected commit.
  - I cannot quantify it without running, which is forbidden.

**F5. CPU cost of the current code.** Eight LPT passes over up to n items with `loads.index(min(loads))` is O(8·n·k). It is negligible for n <= 1620, but it is also wasted work whenever LB(k) >= the best cost found so far.

## (b) Risks and waste

- Recall risk from this change: none. The selected set is identical. Every selected id still appears exactly once.
- Merge risk: workers that end up empty. The new code only moves items from a max-load worker to another worker, and only when the target stays strictly below the old max. A worker cannot become empty, because `lb + x < top` with `x = top` is impossible. As before, `if b` filters anyway.
- Cost waste today: the F4 gaps above, plus wasted evaluation of k values that cannot win.
- Determinism: all tie-breaks are by test number, bin index and a total-order key tuple. There is no set iteration or randomness.
- Improvement CPU: bounded to `IMPROVE_MOVES` moves. Each move scans max-load bins x other bins x items, using bisect so there is no inner item-by-item loop. It stops immediately when the bound is reached, which is the common case with a giant.

## (c) Code (drop-in for selector.py:463-481)

Add `import bisect` to the imports. Replace the whole `split()` with the following.

```python
IMPROVE_MOVES = 64  # cap on move/swap steps per k


def _lb_ms(d: list[int], total: int, k: int) -> int:
    """Lower bound on the max load for k workers (d sorted descending)."""
    v = max(d[0], -(-total // k))
    if k < len(d):
        v = max(v, d[k - 1] + d[k])  # pigeonhole: k+1 items on k workers
    return v


def _lpt(order: list[int], ms: dict[int, int], k: int):
    loads = [0] * k
    bins: list[list[int]] = [[] for _ in range(k)]
    for n in order:
        i = loads.index(min(loads))
        bins[i].append(n)
        loads[i] += ms[n]
    return bins, loads


def _improve(bins, loads, ms, floor: int) -> None:
    """Move/swap an item off a max-load worker while the pair's max strictly drops."""
    k = len(bins)
    for _ in range(IMPROVE_MOVES):
        top = max(loads)
        if top <= floor:
            return
        srt = [sorted((ms[n], n) for n in b) for b in bins]
        keys = [[v for v, _ in s] for s in srt]
        best = None
        for mi in range(k):
            if loads[mi] != top:
                continue
            for bi in range(k):
                gap = top - loads[bi]
                if bi == mi or gap < 2:
                    continue
                kb = keys[bi]
                for x, xn in srt[mi]:
                    if x == 0:
                        continue
                    cands = [(x, None)]  # plain move: d = x
                    j = bisect.bisect_left(kb, x - gap // 2)  # swap partner near x - gap/2
                    for t in (j - 1, j):
                        if 0 <= t < len(kb):
                            cands.append((x - srt[bi][t][0], srt[bi][t][1]))
                    for d, yn in cands:
                        if 0 < d < gap:
                            key = (max(top - d, loads[bi] + d), mi, bi, xn, -1 if yn is None else yn)
                            if best is None or key < best[0]:
                                best = (key, d, mi, bi, xn, yn)
        if best is None:
            return
        _, d, mi, bi, xn, yn = best
        bins[mi].remove(xn)
        bins[bi].append(xn)
        if yn is not None:
            bins[bi].remove(yn)
            bins[mi].append(yn)
        loads[mi] -= d
        loads[bi] += d


def split(selected: set[int], ms: dict[int, int]) -> list[list[int]]:
    """Cheapest split: LPT + improvement passes per worker count, stopping at the lower bound."""
    if not selected:
        return []
    order = sorted(selected, key=lambda n: (-ms[n], n))
    d = [ms[n] for n in order]
    total = sum(d)
    kmax = min(MAX_WORKERS, len(order))
    lbs = {k: _lb_ms(d, total, k) + WORKER_MS * k for k in range(1, kmax + 1)}
    best = None
    for k in sorted(lbs, key=lambda k: (lbs[k], k)):
        if best is not None and best[0] <= lbs[k]:
            break  # sorted by bound: no remaining k can win
        bins, loads = _lpt(order, ms, k)
        _improve(bins, loads, ms, lbs[k] - WORKER_MS * k)
        cost = max(loads) + WORKER_MS * k
        if best is None or (cost, k) < (best[0], best[2]):
            best = (cost, bins, k)
    return [sorted(b) for b in best[1] if b]
```

Why this is correct:
- **Stop rule.** Candidates are visited in ascending `(LB(k), k)`. Any later k has `cost >= LB(later) >= best`. So `best <= LB(k)` proves the best is global-optimal among candidates and ends the loop. When it stops on equality it keeps the earlier k, which is deterministic.
- **Termination of the improvement loop.** Each applied step replaces one max-load bin's load by two values strictly below `top` with the same sum. The sorted-descending load vector therefore strictly decreases lexicographically. A hard cap bounds it anyway.
- **Move condition.** The move is valid when `0 < d < gap`, where `gap = top - lb`. Then `max(top-d, lb+d) < top`. The best d is closest to `gap/2`, and the bisect finds the two partners that bracket it. `gap < 2` is skipped because integer d needs `0 < d < gap`.
- **Hand run of the real example** (3863, 3537, 2533, 2232, 1963):
  - Visit order is k=3 (LB 10765), then k=2 (LB 11064), then k=4 (LB 12195).
  - k=3: LPT gives 3863 / 5500 / 4765. No valid move exists, so the cost is 11500.
  - k=2: best=11500 > 11064, so LPT runs (6095 / 8033). Swapping 3537 for 2232 (d=1305 < gap 1938) gives 7400 / 6728, cost 11400.
  - k=4: LB 12195 >= 11400, so it breaks. The result is **11400**.
- **Hand run of the 5,5,4,4,3,3,3 example** (k=3):
  - LPT gives 11 / 8 / 8.
  - Move 1: swap a 5 for a 3, giving 9 / 10 / 8 (bin B has the lower index than C, so it wins the tie).
  - Move 2: swap 5 for 4, giving 9 / 9 / 9, which equals the bound of 9, so it stops.

## (d) Ranked recommendations

1. **Adopt the code above.** It has zero recall risk, is deterministic and is cheap. It gives 0 to 12% off split cost on selections without a dominant test. It is never worse than the current split, because LPT is still the starting point for each k, the loop only applies strictly improving steps, and the extra candidate order only prunes k values whose bound is already beaten.
2. **Keep the LB-sorted k order with early break.** It removes up to 7 of the 8 LPT passes for most selections. With a giant, typically only 1 to 2 k values are evaluated, which saves CPU, and the container CPU time is part of the score.
3. **Optional, small gain:** for n <= ~12 and a remaining gap, an exact DP or branch-and-bound over k=2 or k=3 partitions would close the final gap, such as the 11500 vs 10765 bound above. It is cheap at that n but adds code. I do not recommend it unless the sample shows a measurable residual.
4. **Do not raise worker count for T < 4000 ms.** The table in F3 says k=1 is right, and both old and new code already do that.

Because I may not run the scorer, the expected-impact figures in F4 come from hand-computed constructions and one hand-checked set built from real durations. They are not a measured sample-wide gain. Someone with run permission should compare `cost(old)` and `cost(new)` over the 30 sample commits. The new cost must be <= the old cost on every commit.
