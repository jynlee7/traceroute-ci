# 15: Is a slow test redundant when cheap tests cover the same changed lines?

Method: read-only Python over `maps/impact.json.gz`, `sample/tests.json`, `sample/answers.json`, `sample/commits/*.patch` and `work/out.jsonl`. I ran nothing from the repo. "Cheap" means under 1 s, "slow" means 1 s or more. "Covered lines" counts (file, line) pairs, 53,747 in total. Test numbers come from `sample/tests.json`. Where I say "dominator" I mean a cheap test that covers every line the slow test covers inside the changed function.

## Short answer

**No defensible rule of the form "cheap tests in the same class or file cover the changed lines, so drop the slow test" exists in this data.** The naive version would have dropped tpcds in both 2jrq and 3utz, which is 2 misses and +600 s. Every refinement I tried that uses coverage overlap still drops tpcds there. The reason is structural, not a matter of tuning:

1. **Scoring counts failing tests one by one.** `SCORING.md:46`: a commit is a miss when at least one of its failing tests is in no worker. If tpcds fails, selecting a cheap sibling that also fails does not cover tpcds. Coverage overlap says the lines are executed by both, but not that they fail together.
2. **Line coverage has no notion of "observes the same API".** A cheap test that executes a line does not necessarily check the output that the slow test checks.
3. **In both real tpcds failures, the cheap tests in the same file cover the changed line and also fail.** The rule fires in exactly the cases where it is wrong.

My recommendation is not to implement this rule. The sections below give the numbers, then the narrower things that are safe.

## (a) Findings

### F1. Per-test coverage table (12 slowest tests)

Columns:
- cov: lines the test covers.
- only: lines covered by that test alone.
- onlyslow: lines covered only by slow tests (including itself).
- cheap-shared: lines also covered by at least one cheap test.
- same-class: lines also covered by another test in the same class (any speed).
- med: median size of the coverage set over its lines, in tests.

| test | s | cov | only | onlyslow | cheap-shared | same-class | med |
|---|---|---|---|---|---|---|---|
| test_parser::test_max_nodes | 28.8 | 867 | 0 | 0 | 867 (100 %) | 864 | 1368 |
| test_executor::test_execute_tpcds | 21.5 | 8899 | 1004 | 1017 | 7882 (89 %) | 7360 | 66 |
| test_generator::test_generator_memory | 6.7 | 286 | 0 | 0 | 286 | 86 | 65 |
| test_executor::test_execute_tpch | 3.9 | 1769 | 0 | 0 | 1769 | 1769 | 689 |
| test_executor::test_optimized_tpch | 3.5 | 2509 | 0 | 0 | 2509 | 2509 | 609 |
| test_optimizer::test_scope | 2.5 | 4065 | 4 | 4 | 4061 | 4053 | 247 |
| test_optimizer::test_tpcds | 2.2 | 6699 | 0 | 13 | 6686 | 6666 | 122 |
| test_optimizer::test_annotate_funcs | 2.0 | 10501 | 171 | 172 | 10329 | 9683 | 8 |
| test_optimizer::test_qualify_columns | 1.4 | 8567 | 94 | 98 | 8469 | 8003 | 99 |
| test_executor::test_agg_order | 1.4 | 2423 | 0 | 0 | 2423 | 2423 | 374 |
| test_optimizer::test_optimize_in_nulls | 1.3 | 5270 | 3 | 5 | 5265 | 5266 | 180 |
| test_schema::test_cache_does_not_retain_queries | 1.3 | 1173 | 1 | 1 | 1172 | 1137 | 1231 |

Reading the table:
- Almost every line a slow test covers is also covered by a cheap test. Only 11 % of tpcds lines are not, and those are the proxy-inflated executor files in F3. The test_annotate_funcs "only" lines are fixture-driven and the median coverage set there is 8 tests.
- High coverage overlap is the normal state, so "overlap with cheap tests" cannot discriminate anything. A rule that keys on it would fire on nearly every selection.
- The cheap-shared lines of the slow tests almost entirely coincide with the common parser, tokenizer and `expressions/core.py` code. For test_max_nodes the median coverage set is 1368 tests.

### F2. Where tpcds's own lines sit

- 8899 lines. The cheap-coverer count per line has quantiles 5 % = 0, 10 % = 0, 25 % = 9, 50 % = 62, 75 % = 230.
- Lines covered only by slow tests (the 1017 above), by file:

| file | tpcds lines | slow-only lines |
|---|---|---|
| `sqlglot/planner.py` | 501 | 288 |
| `sqlglot/executor/python.py` | 558 | 204 |
| `sqlglot/executor/env.py` | 336 | 191 |
| `sqlglot/executor/table.py` | 158 | 103 |
| `sqlglot/generators/python.py` | 146 | 81 |
| `sqlglot/executor/context.py` | 113 | 71 |
| `sqlglot/executor/__init__.py` | 91 | 66 |

- These are the lines `tools/build_map.py:166-181` stamps with `tpc_exec` (the "whole executor" proxy). They come from the proxy, not from measured coverage, because the spawned children are not traced (`research/11` F3).
- So for executor, planner and python-generator edits, a "cheap test covers it" claim is only as good as the proxy, and it is false for about 60 % of those lines (no cheap test covers them at all).

### F3. The two real tpcds failures, checked against the rule

Patches:
- 2jrq: `sqlglot/optimizer/eliminate_joins.py:68`, `and not join.args.get("on")` becomes `or not ...`.
- 3utz: `sqlglot/serde.py:53`, `if node.type and node.type is not node` becomes `or`.

| | 2jrq | 3utz |
|---|---|---|
| tests covering the changed line | 10 (7 optimizer, 3 executor) | 181 (167 optimizer, 5 serde, 5 teradata, 2 executor, ...) |
| failing tests among them | 8 of 10 | 37 of 181 |
| cheap coverers of the changed function that are line-supersets of tpcds there (dominators) | 8 | 160 |
| the dominators include the same-file/same-class tests that also fail | yes: test_eliminate_joins, test_optimize, test_optimize_special_joins, test_tpch, test_execute_subqueries | yes: 36 test_optimizer tests, 5 serde tests |
| what the naive rule does | drops tpcds (fails) | drops tpcds (fails), also drops tpch (fails) and test_tpcds (fails) |

The tpcds numbers are not special. Coverage dominance holds for the failing cases as well as the harmless ones:

| commit | edit | tpcds covers function lines | cheap dominators in function | tpcds fails |
|---|---|---|---|---|
| 2jrq | `and` to `or` in eliminate_joins | 8 of 12 | 8 | yes |
| 3utz | `and` to `or` in serde.dump | 52 of 52 | 160 | yes |
| 2haz | `is` to `==` in Dialect.normalize | 7 of 10 | 63 | no |
| 66ks | `is None` to `== None` in context.py | 12 of 12 | 0 | no |
| 4d63 | `NUMERIC_ESCAPES` value in a Snowflake tokenizer table | 0 of 52 | n/a (tpcds does not run the line) | no |

- Dominance holds for both failing commits and for 2haz, the harmless one. It therefore carries no information about whether the slow test fails.
- The same shape holds for test_execute_tpch (fails in 3utz, covered by 1769/1769 lines elsewhere), test_tpcds in optimizer (fails in 2jrq and 3utz, cov 6686/6699 shared) and test_canonicalize.
- The failure-hit rate among covering tests is not a usable signal either: 2jrq 8/10 (80 %), 3utz 37/181 (20 %), 2haz 0/81, 66ks 0/50. It is only visible after running the tests.

### F4. Refinements I tried, and why each one still drops tpcds in 2jrq/3utz

| refinement | would it drop tpcds in 2jrq/3utz? | why |
|---|---|---|
| R0: every changed line covered by S is also covered by a cheap test | yes | F3 (both have 8 and 160 cheap dominators) |
| R1: require same class or file (test_executor / test_optimizer) | yes | all covering cheap tests in both commits are in test_optimizer, and 3 of the 2jrq ones are in test_executor, tpcds's own file |
| R2: require at least k cheap dominators | yes for k up to 8; but 2jrq has only 8 vs 2haz 63, so any k that spares 2jrq spares nothing else | the count tracks how central the code is, not whether S fails |
| R3: require cheap coverage to include a test that "asserts the same output kind" (SQL text vs executed values) | yes in both | the cheap executor tests (test_execute_subqueries, test_set_operations) do compare executed values and cover the same lines |
| R4: require the cheap coverers to exercise as many distinct inputs as S | not decidable | the map has per-test strings (`test_strings`), but no per-line hit counts, so "inputs reaching this line" is not available |
| R5: drop S only when S executes no changed line (not coverage overlap) | no (tpcds is kept in both, since it covers the line) | this one is safe, but it is plain coverage selection, which `selector.py` already does (SLOW_MS rule at `selector.py:32-34`) |

R5 is the only variant that cannot drop 2jrq/3utz. It is not a redundancy rule, only the existing coverage rule.

### F5. What a sound redundancy rule would need, and why it is out of reach

For S to be redundant on a patch, three conditions must hold:
1. Everything S asserts must also be asserted by some selected cheap test (assertion-set containment).
2. Every input that reaches the changed line in S must also reach it in the cheap test (value coverage, not line coverage).
3. The cheap test must be selected, which costs time.

Line-level coverage alone gives none of them. tpcds's distinguishing power is the second one, 99 queries with 57 executed against duckdb (`research/11` F3). tpch's is the same: its parent-side pickling goes through `serde.dump` (F2 of `research/11`).

Corollary: the slow tests cannot be dropped because other tests are present. They can only be dropped when they provably do not run the changed lines (coverage) or the edit is semantically inert (`is None` to `== None`, comments, docstrings).

### F6. The slow tests that are actually selected without covering the changed line

I checked the changed lines of the sample commits that select generator_memory. For 4d63, 535z, fryt and gbav, test_generator_memory covers **0** of the changed lines (checked per hunk; 4, 4, 4 and 7 covered lines in the hunks, none of them in its set).
- It is selected by the module and import-time rules, not by line coverage. That is a possible narrowing target, but it belongs to the import-time analysis (`research/20`), not to redundancy.
- Same finding for 4d63 and tpcds: tpcds covers none of the 52 lines in `Tokenizer` in `snowflake.py:131-190`; the changed line is covered by 4 tests. tpcds is in 4d63's selection because of the module-level rule, not coverage. The same holds for test_execute_tpch, test_optimized_tpch, test_agg_order, test_scope and test_tpcds in that commit.
- Selection rates in the last run (`work/out.jsonl`), as selected / failed / selected-and-failed:
  - tpcds 5 / 2 / 2
  - generator_memory 4 / 0 / 0
  - tpch 2 / 1 / 1
  - optimized_tpch 1 / 0 / 0
  - test_scope 3 / 0 / 0
  - optimizer test_tpcds 4 / 2 / 2
  - max_nodes 0 / 0 / 0
- The hits come only on edits that really change semantics (eliminate_joins, serde). Misses happen only where the edit is equivalent or does not reach the code. That points at edit-equivalence and reachability, not at coverage overlap, as the right lever.

## (b) Estimated savings and risk

| option | sample saving | recall risk |
|---|---|---|
| Implement R0 or R1 (drop slow when a same-file cheap test covers the lines) | up to roughly 20 s per commit on 2haz, 66ks, 4d63, 2jrq, 3utz, about 100 s total | **2 misses on the sample (2jrq, 3utz) = +600 s.** Net loss of about 500 s. Hidden set 1.5x larger: expect about 3 such commits. Reject. |
| Keep the slow tests as now (coverage-selected) | 0 | none beyond the existing map risk |
| R5 narrowing only (do not select S when the changed lines are not in S's coverage and no import-time reason) | up to about 21.5 s each on 4d63 (tpcds, tpch, optimized_tpch, agg_order, scope, test_tpcds) | tpcds proxy lines (F2) make "not in coverage" true only for non-executor files; for executor files keep S |

Patch shapes that would miss under the redundancy rule:
- Any `and`/`or`, `is`/`==`, `<`/`<=`, `not` flip in optimizer, planner or executor code on a path the cheap fixture tests hit only with trivial inputs. tpcds, with its 99 queries, hits it with many.
- A serde edit: the cheap serde tests passed up to 5 lines, yet 3utz still failed 37 tests including the 2 slow executor ones.
- An edit to a fixture-driven function where the cheap test only runs a few SQL strings.

## (c) Code changes

No change recommended for the redundancy rule. If someone still wants to try R0, here is a guard that makes it effectively inert, so the experiment can be reasoned about, not shipped:

```python
# selector.py, inside the slow-test gate (near SLOW_MS use, selector.py:32-34, 197).
# NOT RECOMMENDED. Shown only because the lead asked for the refinement to be tried.
# Condition under which a slow test S could be dropped at all:
def droppable_slow(self, s, changed_cov_sets):
    # S must execute no changed line (then it is not redundant, it is irrelevant),
    # which the existing coverage selection already handles.
    return all(s not in cs for cs in changed_cov_sets)
```

For the real finding in F6 (slow tests selected without covering the changed line), the safe tightening belongs to the module/import-time rule. The test to apply there is: only select S through a module-level or import-level path if S's own coverage includes at least one line of the changed *file* outside the changed hunk. For 4d63, tpcds covers 52 lines of `snowflake.py`'s dialect module that are not the changed class table, so it would still be selected. A tighter test, "S covers at least one line of the changed *class body*", would drop tpcds in 4d63 but needs the import-time author's agreement (`research/20`); I did not verify that tighter test against any other commit.

## (d) Ranked recommendations

1. **Do not add a "cheap tests overlap, drop slow" rule.** Saves nothing net, costs +600 s on the sample (2jrq, 3utz). Risk: very high.
2. **Keep tpcds, tpch, optimizer test_tpcds selected whenever their coverage (including the executor proxy lines) hits a changed line.** This is what the selector does. 0 s saved, risk low. It protects 2jrq and 3utz.
3. **For executor, planner and python-generator edits, trust the proxy only as an over-approximation (F2).** About 60 % of tpcds executor lines have no cheap coverer, so overlap claims there are false. 0 s saved, risk avoided.
4. **Investigate class-body-level reachability for the module-level selection of slow tests (F6, 4d63/535z/fryt/gbav).** Potential saving: about 7 s on each of fryt, gbav, 535z (generator_memory only) and about 20 s on 4d63. Risk medium (import-time crashes, `research/20`). Needs the owner of that analysis.
5. **Save time through edit-equivalence, not coverage overlap.** Edits like 2haz (`is` to `==` on an enum) and 66ks (`is None` to `== None`) do not fail tpcds. A general principle (an AST edit that is semantically identical for the types involved) is the right way to skip these. It is outside this topic. Potential saving about 20 s each on 2haz and 66ks. Risk medium: `==` vs `is` differs when `__eq__` is overridden.

## 5-line summary

1. A "cheap same-file tests cover the changed lines, so drop the slow test" rule is not defensible: coverage dominance holds in 2jrq and 3utz, so it would drop tpcds there (2 misses, +600 s).
2. Dominance also holds in the harmless commits (2haz: 63 cheap dominators), so it carries no information about whether the slow test fails; the cheap siblings that cover the line are exactly the ones that fail alongside tpcds, and each failing test must be selected.
3. Every refinement via counts, same class, same output kind still drops tpcds; the only variant that spares it is plain coverage selection, which the selector already does.
4. The real slack is in selections where the slow test does not execute the changed line (4d63, generator_memory in fryt/gbav/535z), plus edit-equivalence cases (2haz, 66ks); those are separate topics.
5. About 60 % of tpcds's executor lines have no cheap coverer (proxy lines), so for executor/planner edits the redundancy argument is also false on its own terms.
