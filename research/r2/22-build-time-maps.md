# 22: Build-time and offline maps that make runtime selection sharper

Question: build CPU is free (4 cores, 8 GB, 10 min, network), and the repo can commit up to 20 MB. What extra map, made from the pinned commit only, would let `selector.py` drop tests it keeps today, without risking a miss?

Nothing was run for this report. All numbers below come from reading the source and doing arithmetic on `sample/tests.json`.

## 0. Facts that set the constraints

- **The whole suite is cheap to run once.** 1,620 tests add up to 103.0 s serial (`sample/tests.json`). `tests/test_executor.py` is 31.6 s of that, `test_parser.py` 29.1 s and `test_optimizer.py` 19.8 s. An instrumented run that is 3–5× slower still takes about 80–130 s with `pytest -n 4`. pytest-xdist 3.8.0 is in `requirements.lock:30`, so this fits the 10-minute build. It can equally be done offline and committed, which is what I recommend (§4).
- **Where the sample's excess cost sits** (from `work/why.log` and the BRIEF table):
  - Operator-swap commits that fail nothing but still pull in tpcds: `2haz` 25.5 s (`is` → `==`, `dialect.py:1279`) and `66ks` 24.8 s (`is None` → `== None`, `executor/context.py:53`). `2qn5` (`<` → `<=` on `version`) costs 2.3 s.
  - Class-body constant edits that take a whole dialect family plus `all_dialect_tests`:
    - `4d63`: 27.5 s, Snowflake `NUMERIC_ESCAPES["u"]` value;
    - `535z`: 11.2 s, TSQL `WEEK_OFFSET`;
    - `gbav`: 10.1 s, SingleStore `SHOW_PARSERS["GROUPS FOR USER"]` value;
    - `ktz3`: 7.1 s, `TriggerProperties.arg_types["constraint"]`.
  - A closure body behind a parser-table entry: `fryt`, 9.2 s (`build_date_delta_with_cast_interval._builder`, `parsers/dremio.py:39`).
  - Together these are about 117 s of the 217.7 s. Of the seven commits above, only `535z` fails a test (1).
- **Line coverage is already exact about *which tests execute a line*.** What it can't tell:
  - (a) whether the edit changes the line's *outcome* for that test (operator swaps, equivalent rewrites);
  - (b) *which entry* of a class table a test reads, because entries share one factory body (`rename_func`, `_show_parser` at `parsers/mysql.py:55`, `build_date_delta*`) or are plain data (`NUMERIC_ESCAPES`, `WEEK_OFFSET`, `arg_types`);
  - (c) which tests *use* a class, since `class_tests` approximates that from lines naming it (`selector.py:309-325`).

  Every candidate below should be judged against (a), (b) and (c).

## 1. Candidate by candidate

### 1.1 Branch/arc coverage (`coverage run --branch`)
- **Gain: about 0 s on the sample, small on hidden commits.** An edited condition line runs for every test that reaches it, and both outcomes matter, so arcs add nothing for operator swaps (`8y7j`, `fryt`, `2qn5`). Bodies of branches are already separated by line coverage. Arcs help in one case only: an *insertion* between two statements, where the tests running the new line are exactly those on arc `a → b`. Today `WINDOW=2` (`selector.py:30`) takes the union of the neighbouring lines. Arcs are line-level, so they are blind to single-line ternaries, `and`/`or` short-circuits and comprehensions, which is where most of the sample's conditions live.
- **Cost:** about 1.3× the coverage run. Size about 2× the line table (~1.5 MB gz).
- **Use:** for an insertion after old line `a` with next old line `b`, select `arc_tests[(a, b)]` instead of `cov(a-2..b+2)`.
- **Verdict: low priority.** There is no insertion commit in the sample to gain from.

### 1.2 Per-test dynamic call-graph contexts
- **Gain: about 0 s beyond line coverage.** Coverage contexts already give every test that executes a callee. Caller → callee edges only help when the question is "which entry point created *this closure instance*". That is case (b), and §1.4 answers it more directly by recording closure identity.
- **Cost:** a `sys.setprofile` run is about 5–10× slower. Size is big: 20k+ edges × tests.
- **Verdict: skip.** §1.4 subsumes it.

### 1.3 Per-test sets of instantiated classes (Expression / Tokenizer / Parser / Generator / Dialect)
- **Gain:** exact for (c). Record `type(self)` in `Expr.__init__` and in the `__init__` of `Tokenizer`, `Parser`, `Generator` and `Dialect`. Then "tests that used class C or a subclass" replaces several approximations: `class_tests` from referencing lines, `file_cov(family)` and the 130+-test name match (`selector.py:272-290`).
  - `ktz3`: the `arg_types` → `required_args` derivation (`expressions/core.py:110`) runs at class creation, so value-level tracking can't see it. Only a test that builds or validates a `TriggerProperties` can notice the change. 134 tests → the handful that instantiate it, 7.1 → ~2.1 s.
- **Cost:** two hooks, overhead under 1.2×. Size: about 1,500 classes × interned sets, ~150 KB gz.
- **Use:** see the `class_users()` snippet in §3. It needs one static guard: tests that enumerate every subclass (grep `tests/` for `__subclasses__`, `subclasses(`, `ALL_FUNCTIONS`, `for name in Dialects` as at `test_generator.py:20`) are always added. The index already has `all_dialect_tests`.

### 1.4 Value-flow map: class-table entry reads and class-attribute reads
**This is the main recommendation.** After all dialects are imported, swap every UPPERCASE class-level dict on `Tokenizer`/`Parser`/`Generator`/`Dialect` subclasses (plus `arg_types` on `Expr` classes) for a recording `dict` subclass, and every scalar UPPERCASE class attribute for a recording non-data descriptor. Each read is logged as `(owner, attr, key)` against the current test.

- **Reads are visible at the right granularity:**
  - `numeric_escapes.get(...)` happens at `tokenizer_core.py:1219` on the very dict object handed over at `tokens.py:606`, with no copy (the tokenizer is pure Python; `sqlglotrs` is not in `requirements.lock`).
  - `SHOW_PARSERS` is read per key through `_find_parser` (`parser.py:10083`).
  - `WEEK_OFFSET` is read inside functions (`dialect.py:2287`, `simplify.py:545`, `generator.py:6768`), never in a metaclass.
- **Key origin comes from the AST, not from value identity.** Key `k` in `C.X` belongs to `C` when it is written literally in C's dict literal. Otherwise it is inherited from the base dict unpacked with `**Base.X`. Value identity would mis-attribute small ints and strings that a child re-states with the same value, and that would cause misses.
- **Bulk reads count as reads of every key.** Iteration, `len`, `copy` and `**d` are recorded as key `"*"`. Overriding `__iter__` is enough to turn off CPython's `PyDict_Merge` fast path, so `**d` and `dict(d)` go through `keys()` and get recorded.
- **Gain on the sample:**

| Commit | Today | With the map | Basis |
| --- | --- | --- | --- |
| `4d63` | 27.5 s | `[]` to ~2.5 s | readers of Snowflake `NUMERIC_ESCAPES` key `u` |
| `535z` | 11.2 s | ~2.5 s | readers of `TSQL.WEEK_OFFSET`, which must include the 1 failing test |
| `gbav` | 10.1 s | `[]` to ~2.2 s | readers of that one `SHOW_PARSERS` key |
| `fryt` | 9.2 s | ~2.2 s | callers of Dremio `FUNCTIONS["DATE_ADD"/"DATE_SUB"]` closures |
| `ktz3` | 7.1 s | ~2.1 s | via §1.3 |

  That totals **about 40–55 s**. `6ze8` (already handled by the lambda rule) might drop another ~1 s.
- **Cost:** one instrumented suite run, about 1.5× (the only overhead is on table reads), plus the thread-pool rerun of `test_optimizer`/`test_execute_tpcds` that the coverage build already does (`tools/coverage_executor.sh`). Size: 1,229 class-body dicts with 7,607 literal entries (counted with `ast` on `work/sqlglot/sqlglot`), plus about 2k scalar attributes, in interned sets: **~0.5–1.5 MB gz**.
- **Use (only for value-only edits):** the old and new class bodies differ only inside the *value* of one entry, the key and the key set are unchanged, and the new value is import-safe (literal-evaluable, or a call to a "pure factory" whose body outside nested defs is only `def`/`return`, like `_show_parser`). In that case select `readers[(owner, attr, key)] ∪ readers[(owner, attr, "*")]` for the owner and every subclass that inherits the key. Fall back to today's rule in any of these cases:
  - the attribute was read with no test context, i.e. at import or class creation. This covers `SHOW_TRIE` (`parser.py:2032`), `_KEYWORD_TRIE`, `required_args` and similar;
  - a key is added, removed or renamed;
  - a key expression can't be resolved statically.

### 1.5 Comparison-site value profile (outcome replay)
**This is the second recommendation.** Rewrite each of the 2,609 `ast.Compare` nodes in a *profiling copy* of sqlglot to `_cmp(site, left, right, op)`. The call records a small abstraction of the operands and returns the original result. Each `BoolOp` (2,758 of them) records the truthiness of each operand it evaluated.

- **What is stored per (site, test):**
  - small primitives, exactly: `None`, bool, int, float, short str, tuples of these, and Enum members as `(cls, name, mixin-value)`;
  - any other object as `("obj", type, eq_is_identity)`;
  - at most 32 distinct samples per site, with an overflow flag.
- **At runtime**, when a patch changes a single Compare node's operator, or a literal comparator, and leaves the operand ASTs unchanged, the selector evaluates the old and new comparison on the recorded samples. A test is dropped *at that site* only if none of its samples changes result.
  - `2haz`: the left operand is always a `NormalizationStrategy` member, because strings are converted at `dialect.py:1173`. `NormalizationStrategy` is a `str` enum (`dialect.py:137`), so `is` and `==` agree on every sample. Result: `[]`.
  - `66ks`: `executor/table.py` defines no `__eq__`, and `_table` is `None` or a `Table`, so `is None` and `== None` agree. Result: `[]`.
  - `2qn5`: no sample with `version == (23, 12)` means `[]`.
  - `8y7j` and `fryt` (`==` → `!=` always flips) and `3utz` (`and` → `or` with a falsy left side) stay selected. That is correct: `3utz` fails 37 tests.
- **Gain: about 50 s on the sample** (25.5 + 24.8 + 2.3). The hidden set's mutation-style edits look the same: 9 of the 16 sample code commits are comparison or boolean-operator swaps.
- **Cost:** about 3–5× suite time because every comparison becomes a Python call: ~100–130 s on 4 cores, plus the thread-pool rerun of tpcds, which is *required*. Without it, `test_execute_tpcds` has no samples, would have to stay selected, and the 2haz and 66ks savings vanish. Size: about 2.6k sites × a few samples × interned sets, **~0.5–1 MB gz**.
- **Compliance: medium-low risk, to be flagged.** It stores observed program state at the pinned commit, not failures, and no mutant is ever executed. It is the same kind of data as coverage. It must never be stored as precomputed "(site, operator) → tests". The selector does the evaluation, and the reviewer sees raw observations.

### 1.6 Per-fixture-line test mapping
- **Gain: 0 s on the sample.** Every fixture commit already selects exactly 1 test (`why.log`: `fixture-stem:1`), and a fixture test with subtests is one atomic test. The only real case is a multi-reader fixture such as `tpc-ds.sql`. There, `test_execute_tpcds` (21.5 s) consumes only entries marked `# execute: true` (the comment in `tools/build_map.py`), so an edit to an unmarked entry needs only optimizer `test_tpcds`. That rule is static and needs no build-time map: parse the fixture's header block for the edited entry.
- **Verdict: implement statically if at all.** Worth ~21 s on a hidden tpcds-fixture commit, and nothing on the sample.

## 2. Recall risk (patch shapes that would cause a miss)

| Map | Shapes that could cause a miss | Mitigation |
| --- | --- | --- |
| 1.4 entry reads | (i) A read that bypasses the subclass: C-level `PyDict_GetItem` on the object (e.g. code that does `dict.get(d, k)`), or `vars(cls)["X"]`. (ii) A value derived at class creation from the dict's *values* that we did not mark. (iii) A test whose outcome changes under instrumentation. (iv) A factory call that raises for the new arguments. | (i) and (iii): at build, compare pass/fail with an uninstrumented run; any test that differs gets `"*"` for every attribute. (ii): an attribute read with no test context forces fallback. (iv): accept only literal values or statically pure factories. |
| 1.3 instantiation | Code that reads a class attribute without instantiating, e.g. `exp.X.arg_types` in a test helper. | Union with the static subclass-enumerator tests and with `class_tests` reference lines that carry the class name in a *read* position. |
| 1.5 comparisons | (i) The 32-sample cap overflowed. (ii) A sample abstraction loses information (objects with custom `__eq__`). (iii) A pool test with no samples. (iv) `__eq__` with side effects. | (i), (ii), (iii): keep the test (`overflow → affected`; a test that coverage says ran the line but has no samples → affected). (iv): only `object`/Enum/primitive `__eq__` counts as replayable. |

Fallbacks only add tests, so in the worst case the selector behaves exactly as it does today.

## 3. Code changes

**New file `tools/build_profile.py`** (offline, next to `coverage.sh`). This is a pytest plugin loaded with `-p tools.build_profile`.

```python
import json, gzip, pytest, threading
from collections import defaultdict
CUR = [None]                                   # global, not thread-local: pool reruns use threads
READS = defaultdict(set)                       # (owner, attr, key) -> {test ids}; test None = import
INST = defaultdict(set)                        # class qualname -> {test ids}
def _k(k):                                     # same key spelling the selector derives from the AST
    import enum
    if isinstance(k, type): return "cls:" + k.__name__
    if isinstance(k, enum.Enum): return "enum:" + k.name
    return repr(k)
class RecDict(dict):
    __slots__ = ("_o", "_a", "_origin")        # _origin: key -> owning class (from the AST literal)
    def _r(self, k): READS[(self._origin.get(k, self._o), self._a, _k(k))].add(CUR[0])
    def __getitem__(self, k): self._r(k); return dict.__getitem__(self, k)
    def get(self, k, d=None): self._r(k); return dict.get(self, k, d)
    def __contains__(self, k): self._r(k); return dict.__contains__(self, k)
    def _all(self): READS[(self._o, self._a, "*")].add(CUR[0])
    def __iter__(self): self._all(); return dict.__iter__(self)   # also disables the **d fast path
    def keys(self): self._all(); return dict.keys(self)
    def items(self): self._all(); return dict.items(self)
    def values(self): self._all(); return dict.values(self)
    def __len__(self): self._all(); return dict.__len__(self)
class RecAttr:                                 # non-data descriptor: instance assignment still works
    def __init__(s, v, o, a): s.v, s.o, s.a = v, o, a
    def __get__(s, inst, cls): READS[(s.o, s.a, "")].add(CUR[0]); return s.v
def pytest_sessionstart(session):
    # import every dialect, then patch classes using origins from maps/index.json["table_keys"]
    ...
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    CUR[0] = item.nodeid; yield; CUR[0] = None
```

`pytest_sessionfinish` writes `work/profile.json`. `tools/build_map.py` then interns the test sets into the existing `sets` table and adds two keys to the payload: `"reads": {"owner|attr|key": sid}` and `"inst": {"Class": sid}`. The comparison profile (§1.5) is a second pass with an import hook that runs `ast.NodeTransformer` on `sqlglot/**` (Compare → `_cmp(...)`, BoolOp → `_bool(...)`), emitting `"cmp": {"rel:line:col": [[sample, sid], ...]}`.

**`tools/build_index.py`:** add `table_keys`, meaning for each `module:Qual.Class` and attribute, the literal keys in its own dict. Also add `pure_factories`: module-level functions whose body outside nested defs is only `FunctionDef`/`Return`/`Assign` from arguments.

**`selector.py`:** a new branch in `sqlglot_change`, before the module-level `import_time_change` path (`selector.py:577`).

```python
entry = value_only_entry_edit(old_tree, new_tree, rel, self.index)   # (owner, attr, key) or None
if entry and import_safe(entry.new_value, self.index.get("pure_factories", {})):
    readers = self.reads_for(entry)        # owner + subclasses inheriting key; None if read at import
    if readers is not None:
        out |= readers | self.class_users(entry.owner_cls) & self.enumerator_tests()
        reasons.append("table-entry"); continue

def reads_for(self, e):
    R, out = self.reads, set()
    for owner in self.inheritors(e.owner, e.attr, e.key):            # from index table_keys + subs
        for k in (e.key, "*"):
            sid = R.get(f"{owner}|{e.attr}|{k}")
            if sid is None: continue
            if sid in self.import_read_sids: return None                  # read with no test context
            out |= self.test_set(sid)
    return out
```

For a single Compare edit with identical operand ASTs, in the `fn` branch (`selector.py:544`):

```python
site = self.cmp.get(f"{rel}:{node.lineno}:{node.col_offset}")
if site is not None and not site.get("x"):
    flips = {n for sample, sid in site["v"] if replay(sample, old_op, new_op, new_lit)
             for n in self.test_set(sid)}
    ran = self.cov(rel, [ln])
    seen = set().union(*(self.test_set(sid) for _s, sid in site["v"]))
    hit = (hit & flips) | (ran - seen)          # tests with no samples stay selected
```

## 4. Build in Docker, or offline and commit?

Both maps need the instrumented suite run *plus* the thread-pool reruns already scripted in `tools/coverage_*.sh`. Inside `docker build` that means `pip install -r requirements.lock` (PyPI is allowed), about 2–4 minutes of 4-core work, and a pass/fail comparison run. It fits in 10 minutes, but a flaky or slow test (`test_max_nodes` takes 28.8 s, and `test_generator_memory` uses `tracemalloc`, so instrumentation may disturb it) could fail the build. **Recommendation: build offline the same way `impact.json.gz` is built, and commit the result** (~1–2.5 MB extra, far under 20 MB). Have `docker build` only verify a checksum. The 10-minute budget then stays free, and the map is reviewable.

## 5. Ranked recommendations

| # | Build | Expected savings on the sample | Recall risk | Compliance |
| --- | --- | --- | --- | --- |
| 1 | **Value-flow map (§1.4) with the class-instantiation map (§1.3) in the same run**: table-entry reads, class-attribute reads, instantiated classes | **40–55 s** (4d63, 535z, gbav, fryt, ktz3) | Low: any read at import, any key-set change and any instrumentation mismatch all fall back to today's rule | Low: a coverage-style map of the pinned commit |
| 2 | **Comparison-site value profile (§1.5)**, which needs the in-process tpcds rerun | **~50 s** (2haz, 66ks, 2qn5) | Low–medium: samples are capped and unknown cases keep the test; correctness depends on the operand abstraction | Medium-low: stores raw observations, and the selector does the replay |
| 3 | Static tpc-ds "execute: true" entry rule (§1.6) | 0 s on the sample, ~21 s on a hidden tpcds-fixture commit | Low | Low |
| 4 | Arc coverage for insertions (§1.1) | ~0 s on the sample | Low | Low |
| 5 | Dynamic call graph (§1.2) | ~0 s | n/a | Skip |

Together, #1 and #2 cover about 95 s of the ~117 s gap described in §0. That would take the sample from 217.7 s to roughly 120–130 s with 0 misses. The rest of the way to 100 s depends on other levers (slow-test pruning, `[]` rules) covered by other reports.

**Summary (5 lines)**
1. Build two runtime profiles of the pinned commit offline (instrumented suite run with thread-pool reruns), commit them (~1–2.5 MB), and leave the Docker build free.
2. Top pick: a value-flow map of class-table entry reads, class-attribute reads and instantiated classes, worth 40–55 s (4d63, 535z, gbav, fryt, ktz3), with fallback whenever something is read at import.
3. Second pick: a comparison-site value profile that the selector replays for operator or literal swaps, worth ~50 s (2haz, 66ks, 2qn5). It needs in-process tpcds, and its compliance must be flagged.
4. Branch/arc coverage, dynamic call graphs and per-fixture-line maps gain about 0 s on the sample. Only a static tpc-ds `execute: true` rule is worth adding.
5. Every new rule only narrows inside today's set and falls back to it on anything unknown, so no rule can select less than the profile proves safe.
