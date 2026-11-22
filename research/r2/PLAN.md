# Round 2: merged plan (22 reports, nothing run)

Baseline on the sample: **217.7 s simulated, 0 misses**, plus an estimated 4-5 s of charged CPU.
Oracle floor, meaning the exact cost of each commit's failing tests alone: **94.35 s** (report 08).
**Realistic floor with zero misses: about 115-125 s.** Getting under 100 s would mean returning `[]` on probability estimates for commits whose failing tests would break 50-60% of the time, so a 300 s miss becomes likely. Not recommended.

Every saving below is an estimate from reading code and data; none has been measured.

## Tier 0: recall fixes (must ship; ~0 s on the sample, they prevent 300 s misses)
| # | Fix | Where | Source |
|---|---|---|---|
| R1 | An edit to a `def` signature, default argument or decorator line selects the whole function's coverage, not the ±2 window. In modules that load lazily, coverage credits those lines only to the first test that imported the module: 162 sites, e.g. a `merge_subqueries` default flip misses 93 tests, tpcds among them. Put it before the import-fn branch, and don't apply slow-test pruning to header lines. | selector.py ~509-533 | 20 (H1, C1) |
| R2 | Give lines covered in the `\|setup` context (setUpClass) to every test in that class. Otherwise a `Table.__init__`/`RowReader` mutant misses tpch, optimized_tpch, etc.; TestOptimizer and TestLineage have the same pattern. | tools/build_map.py (map rebuild) | 01 |
| R3 | A function edit in a file that isn't in the coverage map currently returns `[]`. Send it to the import-time rule instead, falling back to ALL. | selector.py 521-533 | 09 |
| R4 | Deleting a `TokenType` member or class attribute that is referenced elsewhere as `Cls.attr` breaks `import sqlglot`: select ALL. | selector.py | 20 (C3) |
| R5 | Seed as import-time any factory called from another file's class body; seed builtin `ENV` dict entries; seed module constants that are read across modules. | tools/build_index.py | 20 (C2, C4, C5) |
| R6 | Optional: a coverage-cookie check, and `\r` lines → ALL. | selector.py | 19 |

## Tier 1: cost cuts confirmed by several agents (low risk), ~65 s on the sample
| # | Change | Sample saving | Risk | Agreed by |
|---|---|---|---|---|
| C1 | **Fix the fixture-name substring match.** `fixture_tests`/`fixture_change` (selector.py:338-344, :634) match by substring, so the stem `optimizer` matches `/optimizer/tpc-ds/` (231 tests including tpcds), and `qualify_columns` or `canonicalize` match 167 tests each. Match the full basename or the last path component, and fall back to substring only when nothing matches. Report 01 checked this against every `.sql` fixture: every match it drops is a false positive, and no fixture ends up with zero readers. | 4d63: −10.7…−14.8 | very low | 01, 03, 04, 11, 12, 13, 14, 18 |
| C2 | **Fix the `_build*` import-time rule.** tools/build_index.py:~232 `name.startswith("_build")` marks ~120 `_builder` closures and `_build_*` functions that run on every parse as import-time. Replace it with: a module-level function whose result is stored in a module-level cache (`_build_dispatch` → `_DISPATCH_CACHE`), or one called at module level or from a class body; a nested closure counts only if its enclosing function is import-time and calls it. | fryt: −7 | very low | 02, 05, 06, 11, 12, 13 |
| C3 | **`is` ↔ `==` / `is not` ↔ `!=` equivalence proof → `[]`.** Allowed when comparing against `None` and the left side's type is proven to have a None-safe `__eq__`: the annotation, plus a build-time scan of every in-repo `__eq__` (only `executor/env.py:64 reverse_key` is unsafe). Also allowed when comparing against a member of a plain Enum, or of a str/int-mixin Enum when every assignment to the attribute is an enum member and no test assigns it. Otherwise fall through. | 2haz −25.5, 66ks −24.8 | low | 04, 07, 09, 22 (01 and 08 agree it's the only way to drop tpcds there) |
| C4 | **Drop construct-only all-dialect tests (`test_generator_memory`) on edits that can't fail at import.** "Import-safe": the AST has the same shape, changed literals keep their type and truthiness, no dict keys change, and any enclosing call only builds and returns a closure. Keep every other all-dialect test, because they assert on values, and keep `test_dialect::test_enum` (0.05 s), which catches import failures. Not for `dialect.py`. | 535z, gbav, 4d63: ~−17 | low | 02, 05, 08, 11, 12, 13 (20 dissents; addressed by the strict guard) |

## Tier 2: precision rules (low to medium-low risk), ~20 s on the sample, more on the hidden set
| # | Change | Sample saving | Risk | Source |
|---|---|---|---|---|
| P1 | **Attribute/value reader rule.** When an edit changes only literal values in a class attribute (scalar, or dict/tuple values with unchanged keys), select the tests covering the lines that *read* that attribute, following one keyword-argument hop. Fall back to today's rule if any reader runs at import time, the value escapes (`getattr`/`**`/`return`), or the type or arity changes. Covers core Generator/Parser flags too (27-37 s → 4-7 s per hidden edit). | 535z → ~2.6, 4d63 → ~3-5 | medium-low | 04, 06, 13, 14 |
| P2 | **`arg_types` value-only flip** → the class's construction-site tests, not whole-file coverage of `expressions/*.py`. | ktz3: −4.7 | low | 05, 11 |
| P3 | **Dead statement → `[]`.** All of these must hold: the touched old lines are absent from the map; the function's `def` line is covered; a `symtable` check shows no scope change and no new yield/await; the file is on an allowlist of dialect-specific files (generators/parsers/dialects except dialect.py, lineage, diff…). Outside the allowlist, use the **tests of the nearest enclosing condition that ran** (report 14, C4), not the class or the whole function. | 8y7j: −2.0 | low | 09, 10, 05, 14 |
| P4 | **Shadowed trie entries** (a SHOW/command key whose complete-key prefix is also a key, so lookup never reaches it) → `[]`. | gbav: −1…−3 | low | 05 |
| P5 | **New-method rule:** count only class methods, and in dialect files keep only tests that use that dialect. A new local `_parse` currently selects ALL. | 0 on the sample (hidden: −37 s when it fires) | low | 11 |
| P6 | **Block-scoped ±2 window:** clip it to the changed line's AST block. | ~0 (hidden: 0.2-2.8 s per core edit) | low | 14 |
| P7 | **TPC fixture expected-half rule:** an edit only to the expected half of a tpc-ds/tpc-h entry, or to an entry without `# execute: true`, drops `test_execute_tpcds`. | 0 (hidden: ~20 s per such edit) | low | 01, 22 |

## Tier 3: CPU (charged, not simulated), ~4 s
A: stub entrypoint plus pyc, lazy load, `gc.disable`, `os._exit`, `-S -E -B` (−0.2 s). B: bake facts about the old files at build time, parse only the new file, use fingerprints instead of `ast.dump` (−1.6-2.0 s). C: re-parse only the changed statement, with a build-time self-test that falls back to the full path (−1.2 s). Bake from the docstring-stripped tree, except for `typing/`. Source: 16.

## Rejected (would cause misses or save nothing)
- Dropping a slow test because cheap tests cover the same lines: it would have dropped tpcds on 2jrq/3utz, i.e. +600 s (15, 14).
- Exact-line pruning on continuation lines or statement heads (the `and`→`or` short-circuit in 2jrq; 2haz line 1279): 03, 01. If `SLOW_MS` is lowered (02, C4), prune only single-line statements.
- `[]` on probability alone (2qn5 `<`→`<=`, 5y72 `copy=` flip, constant flips): expected +150 s (08, 09, 20).
- Gates based on assertion type or observation channel: ~0 gain (12). Reordering, local-rename and f-string rules: never occur (09). Changing the split: already optimal (17).

## Decisions for you
1. **Target:** ~118 s at zero misses (recommended) vs sub-100 s with real miss risk.
2. **Offline instrumented runs** (they break "no local testing", but give the biggest further gains):
   - Re-run `tools/coverage_executor.sh` without xdist, so tpcds gets exact coverage, the proxy becomes a fallback, and the `leave_tables_isolated` gap closes (01, 10).
   - Rebuild the map with the R2 setUpClass broadcast (needed anyway).
   - Optional: the value-flow map (22, Rec 1; ~40-55 s, overlaps P1) and the comparison-value profile (22, Rec 2; it would also make 2qn5 `[]`, but flag it for compliance first).
3. **Compliance (21):** the hand-named tpch/tpcds/test_optimizer tests in build_map.py:120-145 are allowed but should be derived by AST. Make `SLOW_MS` a pin-derived quantile.

## Projected sample score (estimates)
| Commit | Now | After T1+T2 |
|---|---|---|
| 2haz | 25.5 | 0 |
| 66ks | 24.8 | 0 |
| 4d63 | 27.5 | ~3-5 |
| 535z | 11.2 | ~2.6 |
| gbav | 10.1 | ~2-3 |
| fryt | 9.2 | ~2.2 |
| ktz3 | 7.1 | ~2.4 |
| 8y7j | 2.0 | 0 |
| others | 100.3 | ~100 |
| **Total** | **217.7** | **~113-118 s simulated + ~0.4 s CPU** |

Implementation order: R1-R5 → C1 → C2 → C3 → C4 → P2 → P3 → P1 → P4-P7 → CPU A+B. Rebuild the map and index, then `tools/eval.sh` and `make sample` once local testing is allowed.
