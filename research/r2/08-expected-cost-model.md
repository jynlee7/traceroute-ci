# 08: An expected-cost rule for dropping slow tests

Scope: a principled include/exclude rule, `P(t fails | patch) × 300 s × (miss dependence) > marginal cost(t)`. It covers: estimating P from features available at the pin, handling the rule that only one miss per commit counts, a drop-in algorithm for `selector.py`, a variance analysis for about 45 hidden commits, and whether sub-100 s is realistic.
Method: I read `selector.py`, `tools/build_map.py`, `maps/impact.json.gz`, `work/index.json`, `work/out.jsonl`, `sample/*` and the pinned sources, using read-only Python on the data only. I ran nothing from the repo. I computed split costs with my own 10-line LPT over `work/out.jsonl`, not with `split()`.

## (a) Findings

**F1. The sample commits are single-token mutants.** Every code commit edits one line, and the edits are classic mutation operators:
- `is`→`==` (2haz, 66ks)
- `and`→`or` (2jrq, 3utz, ary6)
- `==`→`!=` (fryt, 8y7j)
- `<`→`<=` (2qn5)
- `not x`→`x` (j6uz)
- int, bool or index literal changes (4d63, 535z, gbav, ktz3, bj7g, 6ze8, 5y72)

So "change type" is a well-defined feature: it is the AST delta of one statement. Liveness priors from the mutation-testing literature apply to it directly: most operator mutants are live, while boundary (ROR `<`/`<=`) and identity/equality mutants are often equivalent or rarely observable.

**F2. Exact line coverage already separates the four tpcds events. The open question is liveness, not reach.**

| commit | changed line | \|cov(line)\| | failing tests in cov | tpcds covers the line exactly | tpcds fails |
|---|---|---|---|---|---|
| 2jrq | eliminate_joins.py:71 `and`→`or` | 10 | 8/8 | yes | **yes** |
| 3utz | serde.py:53 `and`→`or` | 181 | 37/37 | yes | **yes** |
| 2haz | dialect.py:1280 `is`→`==` (Enum) | 86 | 0/0 | yes | no |
| 66ks | executor/context.py:53 `is None`→`== None` | 51 | 0/0 | yes (executor "whole" proxy, build_map.py:146-153) | no |

- Wherever there is a failure, every failing test is in the exact coverage of the changed line (sample-wide: 2jrq, 3utz, 535z, 6ze8, ary6, bj7g, j6uz all 100 %). The one exception is rszg 1/3, a multi-file commit whose other failures come from its fixture file.
- So P(t fails | t does not cover the line) is the coverage-gap leak only. In both commits where tpcds covered a live (`and`→`or`) mutant, it failed (2/2).
- The two survivors are both identity→equality swaps that are equivalent here:
  - `NormalizationStrategy` is always built as an Enum member (dialect.py:1169-1173).
  - `Context._table: Table | None` (context.py:33), and `executor/table.py:10 Table` has no `__eq__`.
- Breadth gives no reason to lower P for an output-checking end-to-end test. tpcds covers 8899 lines (the median test covers 1394), yet it caught 2 of 2 live mutants.

**F3. test_generator_memory is a weak oracle.** It only constructs `dialect.generator()` and asserts memory growth under 1 KB (`tests/test_generator.py:19-38`).
- It can fail only if importing the dialect, or `Generator.__init__`, raises or leaks.
- It enters fryt, gbav, 535z and 4d63 only through `all_dialect_tests` (selector.py:409). In all four, exact coverage of the dialect family's lines has **0** generator_memory lines: I counted every line of `dialects|parsers/{snowflake,dremio,tsql,singlestore}.py`.
- None of these edits can make the import or construction raise:
  - 535z, 4d63 and gbav change one literal to another literal of the same type in a class body or call argument (`-1`→`-2`, `16`→`17`, `target=True`→`False`). Evaluating a constant cannot raise.
  - fryt edits a comparison inside the nested `_builder` (parsers/dremio.py:37). That body runs only when a dremio date function is parsed, never at import.
- P(fail) ≈ 0.0005 against a break-even of 1.1-2.2 %, so generator_memory should be dropped in all four.

**F4. tpcds and tpch reach 4d63 through a substring bug, not through reach.**
- The route is `name_tests` → `fixture_dialects["snowflake"]`, which includes `tests/fixtures/optimizer/optimizer.sql` (selector.py:277) → `fixture_tests(stem="optimizer")`.
- `fixture_tests` matches the bare stem as a substring (selector.py:343). So "optimizer" matches `'optimizer/tpc-ds/tpc-ds.sql'` in tpcds's strings, and the same happens for **231 tests**.
- `fixture_change` has the same substring match (selector.py:634).
- tpcds never touches the snowflake tokenizer, which is consistent with 0 tpcds lines in the snowflake family.

**F5. The oracle floor on the sample is 94.35 s, not ~85 s.** I computed the exact cost of each commit's failing set.
- Current over-spend: **111.1 s** on the 18 commits that fail nothing, and **12.2 s** above oracle on the 12 that fail something.
- Getting under 100 s means at most 5.6 s of total over-spend across 28 commits.

## The model

For each changed statement ℓ in sqlglot/ and each candidate test t:

```
p(t) = 1 - Π_ℓ (1 - p(t,ℓ)) ,     p(t,ℓ) = L(ℓ) · D(t) · R(t,ℓ)  + g
```

**L(ℓ), liveness.** A property of the patch alone, derived by comparing the old and new statement ASTs. It is not a test table: it never names a test.

| delta | L | rationale |
|---|---|---|
| AST-identical / docstring | 0 | already returns [] |
| `is`↔`==` or `is not`↔`!=` with a `None` operand, where the other operand's annotated class (index `refs` + `__init__` annotation) defines no `__eq__` | 0.01 | provably equivalent; the ~1 % covers annotation lies |
| same as above, but the type is unknown, or there is an Enum operand (str-mixin Enums such as dialect.py:137 compare equal to raw strings) | 0.05 | usually equivalent |
| `<`↔`<=`, `>`↔`>=` | 0.3 | observable only at equality |
| anything else (negation, `and`↔`or`, literal change, argument change, deletion) | 0.9 | |

**D(t), oracle strength.** Built statically from the test's own AST.
- *Weak* (D = 0.15): every `self.assert*` call is one of `assertLess/LessEqual/Greater/True/False/IsInstance/IsNotNone/Raises` (generator_memory). A weak test fails mainly on a raise.
- *Strong* (D = 0.6, the default): anything that compares a value to an expected one (`assertEqual`, `validate_*`, fixture outputs, executor result comparison). This includes test_max_nodes, whose `assertIn` checks the message text.

**R(t,ℓ), reach.**
- 1.0 if t is in `cov(rel, [ℓ])` (exact, test context).
- 0.05 if t ran only the window or function but not ℓ (coverage-error leak).
- `P_raise(ℓ)` if t came only from an import-time heuristic: `all_dialect_tests`, `name_tests`, family `file_cov`, `class_tests`, where t has no exact coverage in the dialect family. Then:
  - `P_raise` = 0.0005 if the statement does not run at import (it is inside a nested def or lambda), or if old and new differ only in constants of the same type;
  - `P_raise` = 0.3 otherwise;
  - R = 1.
- **Edits in non-dialect base files** (parser.py, generator.py, tokens.py class tables) are *not* discounted: tests read those tables at run time from other lines. In that case t is treated as exact.

**g, the global coverage-gap floor.** 0.002 per test. It is the clamp that stops any estimate from reaching 0.

**Correlation (one miss per commit).** The commit-level penalty is `300 · P(∃ t ∈ E fails)` for an excluded set E, not `Σ p_t`.
- Exact-reach tests share a common cause: the edit is live or it is not. Condition on that:

```
P(miss | E) = L · (1 - Π_{t∈E_exact}(1 - D_t)) + (1 - Π_{t∈E_indirect}(1 - P_raise_t)) + |E|·g
```

- **Consequence 1.** Dropping tpcds *and* optimizer test_tpcds on a likely-equivalent edit costs about L·(1−0.4·0.4) = 0.84·L, not 1.2·L. The union bound would double-count the liveness factor.
- **Consequence 2.** This is all-or-nothing. Once L is small enough to drop the biggest slow test, the other exact slow tests sharing the same L are almost free to drop, and vice versa. So evaluate subsets jointly, not test by test.
- **Consequence 3.** Including a test never offsets the miss from an excluded one. "Miss dependence" applies only *among excluded tests*.

**Decision.** Use a risk aversion κ ≥ 1, the factor by which a miss is treated as worse than 300 s. Choose E ⊆ slow(S) to minimize

`cost(S \ E) + 300 · κ · P(miss | E)`.

**Thresholds this implies.** The marginal cost Δ of tpcds is 15.7-21.5 s in the sample (2haz 15.7, 66ks 21.5). So tpcds is dropped only when P < 5.2-7.2 % at κ = 1, or 1.7-2.4 % at κ = 3.

**[] for a whole commit.** This happens only when `P_any < cost/(300κ)`, about 0.25-0.8 % for a 2.5 s commit. A live covered mutant fails about 60 % of the time (sample: 8 of 13 covered non-equivalent edits), so **[] must come only from L = 0**, meaning a proof of equivalence, never from a probability estimate.

## (c) Code

**1. selector.py:338-343 and 634 — fix the substring match (F4).** Match the fixture *file* name, not the bare stem:

```python
    def fixture_tests(self, fixture_rel: str) -> set[int]:
        path = Path(fixture_rel)
        keys = (path.name, f"{path.parent.name}/{path.stem}")   # "optimizer.sql", "optimizer/optimizer"
        memo = self.__dict__.setdefault("_fx_memo", {})
        if fixture_rel not in memo:
            memo[fixture_rel] = {n for n, ss in self.strings.items()
                                 if any(k in s for s in ss for k in keys)}
        return set(memo[fixture_rel])
```

- Apply the same `keys` in `fixture_change`, keeping the current stem and directory fallbacks when nothing matches.
- **Check before merging:** helpers that load fixtures by bare stem (`load_sql_fixture_pairs(f"optimizer/{name}.sql")` with `name` passed as a string) still match `"optimizer/optimizer"` only if the call site holds the formatted string.
- Grep `tests/test_optimizer.py` for `check_file(` callers. They pass `"optimizer"`, etc. So keep `stem` as a key **only when the string has no `/`** (`any(s == path.stem ...)`).

**2. Track the exact set and liveness while selecting.**
- Add to `Selector.select` at selector.py:642: `self.exact, self.L, self.raise_p = set(), 0.0, 0.0005`.
- Treat every non-sqlglot route's tests as exact: after `out |= s`, add `if not rel.startswith("sqlglot/"): self.exact |= s; self.L = max(self.L, 0.9)`.
- Inside `sqlglot_change`, after `changed = ...` (around line 474):

```python
        self.exact |= self.cov(rel, changed)
        self.L = max(self.L, liveness(f))
        if not import_safe(old_tree, new_tree, changed, self.index.get("lambdas", {}).get(rel, [])):
            self.raise_p = 0.3
        if not rel.startswith(DIALECT_DIRS) or Path(rel).stem in ("__init__", "base"):
            self.base_edit = True        # runtime table reads: no indirect discount
```

**3. Module-level helpers (put them next to `same_indent_replacements`).**

```python
_IDENT = {("is", "=="), ("==", "is"), ("is not", "!="), ("!=", "is not")}
_BOUND = {("<", "<="), ("<=", "<"), (">", ">="), (">=", ">")}

def liveness(f: dict) -> float:
    """Liveness prior of a one-line operator mutant; 0.9 for anything not recognised."""
    pairs = [(m[1], p[1]) for h in f["hunks"] for m, p in _pairs(h["lines"])]
    if not pairs or any(p is None for _, p in pairs):
        return 0.9
    worst = 0.0
    for old, new in pairs:
        a, b = old.split(), new.split()
        diff = [(x, y) for x, y in zip(a, b) if x != y]
        if len(a) == len(b) and len(diff) == 1 and diff[0] in _BOUND:
            worst = max(worst, 0.3)
        elif _ident_swap(old, new):
            worst = max(worst, 0.05)          # 0.01 needs the annotation proof (F2); keep 0.05 by default
        else:
            return 0.9
    return worst

def _ident_swap(old: str, new: str) -> bool:
    for x, y in _IDENT:
        if f" {x} None" in old and old.replace(f" {x} None", f" {y} None") == new:
            return True
    return False

def _pairs(lines):
    """Yield (minus, plus) one-for-one replacement pairs of a hunk; (m, None) otherwise."""
    i = 0
    while i < len(lines):
        if lines[i][0] != "-":
            i += 1; continue
        ms = []
        while i < len(lines) and lines[i][0] == "-":
            ms.append(lines[i]); i += 1
        ps = []
        while i < len(lines) and lines[i][0] == "+":
            ps.append(lines[i]); i += 1
        yield from (zip(ms, ps) if len(ms) == len(ps) else [(m, None) for m in ms])

def import_safe(old_tree, new_tree, changed: set[int], lambdas) -> bool:
    """The edit cannot raise at import: it sits in a def/lambda body, or swaps constants of one type."""
    def shape(t):
        d = ast.dump(t)
        return re.sub(r"Constant\(value=([^,)]+)", lambda m: f"Constant({type(ast.literal_eval(m.group(1))).__name__}"
                      if _lit(m.group(1)) else m.group(0), d)
    if old_tree is not None and new_tree is not None and shape(old_tree) == shape(new_tree):
        return True
    defs = [(n.lineno, n.end_lineno) for n in ast.walk(old_tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]
    # Nested deps: top-level functions may run at import (import_fns); only *nested* bodies are safe.
    nested = [(a, b) for a, b in defs if any(a2 < a and b <= b2 for a2, b2 in defs)] + list(map(tuple, lambdas))
    return all(any(a < ln <= b for a, b in nested) for ln in changed)

def _lit(s: str) -> bool:
    try: ast.literal_eval(s); return True
    except Exception: return False
```

- A `shape()` comparison of the whole module is cheap at this scale; it runs once per file.
- `-1` → `-2` is `UnaryOp(USub, Constant(int))` on both sides, so it is treated as safe.

**4. Joint prune, called in `main()` between `sel.select` (line 936) and `split` (line 939): `chosen = sel.prune(chosen, ms)`.**

```python
RISK = float(os.environ.get("RISK", "3"))           # κ
PRUNE_MS = 1000

    def prune(self, S: set[int], ms: dict[int, int]) -> set[int]:
        slow = sorted((t for t in S if ms[t] >= PRUNE_MS), key=lambda t: -ms[t])[:6]
        if not slow or getattr(self, "L", 1) >= 0.9 and not slow_indirect(self, slow):
            return S
        weak = self.index.get("weak_oracle_set") or set(self.by_id[t] for t in self.index.get("weak_oracle", []) if t in self.by_id)
        self.index["weak_oracle_set"] = weak
        rest = sorted((ms[t] for t in S if t not in slow), reverse=True)
        best = (quick_cost(rest + [ms[t] for t in slow]), ())
        for mask in range(1, 1 << len(slow)):
            E = [t for i, t in enumerate(slow) if mask >> i & 1]
            ex, ind = [t for t in E if t in self.exact or getattr(self, "base_edit", False)], [t for t in E if t not in ex]
            pm = self.L * (1 - prod(1 - (0.15 if t in weak else 0.6) for t in ex)) \
                 + (1 - (1 - self.raise_p) ** len(ind)) + 0.002 * len(E)
            keep = sorted(rest + [ms[t] for t in slow if t not in E], reverse=True)
            v = quick_cost(keep) + 300_000 * RISK * pm
            if v < best[0]:
                best = (v, tuple(E))
        if best[1]:
            log("prune", [self.tests[t]["id"].rsplit("::", 1)[-1] for t in best[1]])
        return S - set(best[1])

def slow_indirect(sel, slow):
    return any(t not in sel.exact for t in slow) and not getattr(sel, "base_edit", False)

def quick_cost(d: list[int]) -> int:      # d sorted descending, ms
    if not d: return 0
    tot = sum(d)
    return min(_lb_ms(d, tot, k) + WORKER_MS * k for k in range(1, min(MAX_WORKERS, len(d)) + 1))
```

- Use `from math import prod`.
- 64 subsets times an O(1) lower-bound cost is negligible CPU. The final exact cost still comes from `split()`.
- Reset `self.base_edit = False` in `select()`.

**5. tools/build_index.py — add `weak_oracle`.** For each `tests/**/test_*.py` method `test_*`, include its id if every `self.assert*` call has an attr in the weak set listed above and the method calls none of `validate_*`, `check_file` or `assertEqual*`. The default is strong, which is the safe side.

## (b) Savings on the sample, and recall risk

My LPT reimplementation, removing only the tests named:

| lever | commits | s saved |
|---|---|---|
| F3: generator_memory via the indirect route, import-safe edits | fryt 6.7, gbav 6.7, 535z 3.3, 4d63 ~1.5 | **~18** |
| F4: fixture substring fix (removes tpcds and tpch, plus about 200 optimizer tests, from 4d63) | 4d63 | **≥ 9** (27.5 → ≤ 16.9 with F3 as well) |
| identity-swap L = 0.05 at κ = 1 (drop tpcds with test_tpcds) | 2haz 15.7, 66ks 21.5 | 37 (**not taken at κ = 3**) |
| same with L = 0.01 (annotation proof) at κ = 3 | 66ks (proof available); 2haz needs dataflow, so no | ~21 |

- Safe total at κ = 3: **~27 s (217.7 → ~190 s)**, or ~48 s with the annotation proof.
- Neither safe lever changes 2jrq or 3utz: L = 0.9, exact reach, p ≈ 0.54, so they are kept.

**Patch shapes that would cause a miss on hidden commits:**
1. A literal swap whose new value is consumed at import by a metaclass that validates it, for example a `KEYWORDS` or `NUMERIC_ESCAPES` value that `_Tokenizer` turns into a trie, or a `TokenType` replaced with a `str`. Same-type swaps exclude the type change, but an *invalid value of the same type* (e.g. a negative base) could still raise in `_Tokenizer.__new__`. Mitigation: treat class-body edits in files whose class has a metaclass that iterates that attribute (index `import_fns` reads) as `P_raise = 0.3`.
2. A change inside `Generator.__init__`, or a generator class attribute it reads. generator_memory covers these exactly, so they take the exact route with D = 0.15 and L = 0.9: p ≈ 0.14, and it is kept.
3. A `== None` on an `Expr`. `Expr.__eq__` (expressions/core.py:837-838) returns False for None, so the edit is equivalent; but `x == other_expr` is live. The `_ident_swap` rule requires a literal `None` operand and gives L = 0.05, never 0. With κ = 3 it never drops a 21 s test on that alone.
4. A wrong `weak_oracle` label is the main systematic risk. Default to strong, and print the weak list in the build log for review.

## Safety analysis: expected score versus variance (~45 hidden commits)

- Misses are Poisson-like with λ = Σ P(miss | E_c) over the commits that were pruned. E[penalty] = 300λ, SD ≈ 300√λ, and P(≥1 miss) = 1 − e^{−λ}.
- Scaling the sample by 1.5×: about 6 generator_memory or heuristic-only events, about 7-8 tpcds exact events (3 truly failing), and about 3 identity swaps.

| policy | E[saved] | λ | E[penalty] | P(≥1 miss) | SD |
|---|---|---|---|---|---|
| κ = 3, structural levers only (F3, F4) | ~40 s | 6 × ~0.003 ≈ 0.02 | 6 s | **2 %** | 42 s |
| κ = 1, plus identity swaps at L = 0.05 | ~95 s | 0.02 + 3 × 0.03 ≈ 0.11 | 33 s | **10 %** | 100 s |
| κ = 1, plus boundary mutants at L = 0.3 | ~120 s | ≈ 0.5 | 150 s | 40 % | 210 s |

- On a ranked board, a miss moves the run from competitive to uncompetitive, so the loss is convex. Use **κ = 3**. That accepts about 2 % miss risk for a ~34 s net gain.
- κ = 1 adds ~27 s of expected value at 5× the miss probability. Take it only if the board requires a gamble.
- Model error is the dominant uncertainty: L and D are priors with n ≤ 13 sanity points. The floor g = 0.002 and κ = 3 are what keep a 2× error from flipping a decision.

## Is sub-100 s realistic without misses? No.

- The oracle is 94.35 s on the sample, which leaves at most 5.6 s of over-spend across 28 commits.
- 9 code commits fail nothing (2haz, 4d63, 66ks, fryt, gbav, 2qn5, 5y72, 8y7j, ktz3). Only 2haz and 66ks are *equivalent* mutants.
- The others survive because the tests are weak (fryt's `== 2`→`!= 2` is covered by 3 tests, none of which notices). That cannot be predicted at the pin: a covered, non-equivalent edit fails about 60 % of the time, against a break-even below 1 % for emitting [].
- So each of those commits keeps at least ~2.0-3.4 s, about 16 s in all, and the failing commits keep about 5-8 s of over-spend even with tight selection.

**Realistic zero-miss floor:** about 94 + 16 + 6 = **~115-125 s** on the sample, if the identity swaps are proven equivalent. Without that proof it is about **~150-165 s**. On the hidden set, expect about 1.5× those figures.

## (d) Ranked recommendations

1. **Fix the fixture substring match (F4)**, selector.py:343 and 634. Saves ≥9 s on the sample (more on hidden, since any dialect edit hits `optimizer.sql`). Risk: low, but check `check_file` callers that pass a bare stem.
2. **Indirect-route discount for weak, import-time tests (F3)**: `exact` tracking, `import_safe`, `prune`. Saves ~18 s. Risk: low; see miss shape 1.
3. **Joint subset prune with the common-cause miss probability, κ = 3**: the framework that encodes 1 and 2 and future levers. Saves 0 s on its own, but prevents per-test double counting. Risk: low.
4. **Equivalence proof for `is None`↔`== None` from annotations and `__eq__` absence (L = 0)**: this makes 66ks `[]`. Saves ~25 s on the sample, ~35 s on hidden. Risk: medium; this needs an index of `self.x: T` annotations and the `__eq__` MRO.
5. **κ = 1 or L = 0.05 identity-swap pruning of tpcds**: saves +37 s on the sample. Risk: **high** (≈10 % miss chance on hidden). Not recommended.
6. **Do not** probabilistically [] boundary or literal survivors (2qn5, 5y72, 8y7j). Expected loss is about +150 s.

Summary:
1. P(t fails) = L(edit liveness) · D(oracle strength) · R(reach) + 0.002, with a common-cause correction because only one miss per commit counts.
2. Exact line coverage already separates the tpcds events, and liveness decides the rest. Both tpcds failures are `and`→`or`, and both survivors are equivalent `is`→`==` swaps.
3. Safe wins at κ = 3: drop generator_memory when it is reached only through import-time heuristics and the edit cannot raise (~18 s), and fix the fixture-stem substring bug that pulls tpcds and tpch into 4d63 (≥9 s).
4. The oracle floor is 94.35 s. Weak-test survivors cannot be predicted at the pin, so the zero-miss floor is about 115-125 s.
5. Sub-100 s is not realistic without accepting a material miss risk.
