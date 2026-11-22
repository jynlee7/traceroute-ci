# R2-14: Edits to high-fanout core modules (parser, generator, tokens, helper, expressions/, optimizer/)

Method: read-only inspection of `maps/impact.json.gz`, `work/index.json`, `work/sqlglot/.coverage*` (sqlite, read-only),
the pinned source and `selector.py`. Costs below come from my own re-implementation of the scorer's formula
(`min_k max(max test, ceil(total/k)) + 2 s·k`, k ≤ 8). It is a lower bound on what `split()` achieves and is usually equal to it.
I ran nothing from the repo. "Slow" means ≥ 5 s (`SLOW_MS`) unless I say otherwise. "Cheap" means < 0.5 s.

## (a) Findings

### F1. On core files, the cost comes from which slow test is selected, not from how many tests are selected
Per covered line, using the exact coverage of that one line (no window):

| file | covered lines | median tests/line | % lines >1000 tests | % with max_nodes | % with tpcds | mean cost, exact line |
|---|---|---|---|---|---|---|
| parser.py | 6076 | 11 | 6 | 8 | 24 | 10.2 s |
| generator.py | 3755 | 6 | 2 | 0 | 19 | 7.5 s |
| tokens.py | 76 | 814 | 50 | 50 | 50 | 23.8 s |
| tokenizer_core.py | 526 | 285 | 33 | 39 | 53 | 20.3 s |
| helper.py | 119 | 25 | 1 | 0 | 65 | 18.1 s |
| expressions/core.py | 730 | 106 | 13 | 6 | 64 | 18.6 s |
| optimizer/scope.py | 503 | 140 | 0 | 0 | 77 | 21.6 s |
| optimizer/simplify.py | 1000 | 7 | 0 | 0 | 60 | 16.5 s |

- The median parser or generator line is covered by only 6–11 tests. Lines covered by more than 1000 tests are rare outside tokens.py and tokenizer_core.py.
- What drives cost is `test_execute_tpcds`, which is on 19–77 % of core lines, then `test_max_nodes`.
- In core files, tpcds coverage is mostly real. I decoded the line bits for `test_execute_tpcds|run` in `.coverage`. Of the core-file lines that carry tpcds:
  - 2405 are measured in the parent process (it runs `parse_one` on all 99 queries and the duckdb transpile);
  - 779 come through the optimizer `test_tpcds` proxy;
  - only 115 come from the tpch proxies alone.
- So dropping a proxy (round-1 report 07) barely changes core edits.

### F2. A class-body or module-level edit in a core file means running everything
`import_time_change` (selector.py:384-415) takes `file_cov(rel)`. That gives:
- parser.py: 1519 tests, 36.8 s;
- tokens.py: 1567 tests, 36.8 s;
- expressions/core.py: 1540 tests, 36.8 s;
- generator.py: 1353 tests, 27.6 s;
- helper.py: 1474 tests, 29.5 s;
- scope.py: 732 tests, 27.5 s.

The whole suite costs 36.8 s. These files hold 211, 94, 192, 172, 29 and 45 class-body or module-level statements, respectively: flags like `Generator.NULL_ORDERING_SUPPORTED`, and tables. The sample has no such commit, but they are plausible on the hidden set.

### F3. 4d63 explodes because of a substring bug, not because of the core
4d63 changes one value in `Snowflake.Tokenizer.NUMERIC_ESCAPES` (snowflake.py:133). I rebuilt the selection from the map and got exactly 561 tests, 27.5 s:
- coverage of the family: 346 tests, no slow test;
- snowflake users (test file and strings): 344 tests, 7.8 s;
- `all_dialect_tests`: 6 tests, including generator_memory;
- `name_tests` → `fixture_dialects["snowflake"]` → `fixture_tests(fx)` (selector.py:338-344). That function matches `stem in s`. For `tests/fixtures/optimizer/optimizer.sql`, the stem "optimizer" is a substring of test_executor's `"/optimizer/tpc-h/"` and `"/optimizer/tpc-ds/"` constants (test_executor.py:30-31, 118-119). That alone pulls in **tpcds, tpch, optimized_tpch, test_scope and test_tpcds**, 231 tests in all. Separately, `qualify_columns` matches `qualify_columns__invalid` and others, pulling in 167 tests.

This is not specific to snowflake. It is the same false channel as round-1 report 11's F9, but at a second call site (`fixture_tests`, used for every dialect class-body edit). The waste it causes for **each dialect's class-body edit**:

| dialect | current fixture-channel tests / cost | path-aware | slow tests dropped |
|---|---|---|---|
| postgres, mysql, duckdb, spark, bigquery, snowflake | 231 / 27.5 s | 12–18 / 6.5–7.0 s | tpcds, tpch, otpch, scope, opt-tpcds |
| clickhouse, presto, redshift, sqlite, oracle, risingwave, starrocks | 167 / 12.6 s | 2–6 / 3.4–6.1 s | scope, opt-tpcds |

With path-aware matching, 4d63 drops from 561 tests / 27.5 s to **367 tests / 12.7 s**. No test globs a fixture directory: there are no `listdir`, `glob(`, `iterdir` or `os.walk` calls under `tests/`. Every loader goes through `load_sql_fixture*("dir/name.sql")` or `check_file("name")` → `f"optimizer/{file}.sql"` (test_optimizer.py:214-226).

### F4. The ±2 window leaks across blocks and brings in slow tests
`WINDOW=2` (selector.py:31, 503) takes lines from sibling or parent blocks, for example the line after an `if` body. I compared three variants over every covered line inside a function:
- exact: the line only;
- window: today's rule;
- block-window: the window limited to lines in the same AST statement list as the changed line.

| file | exact (tests/cost) | window | block-window | whole fn | % lines where window adds a ≥5 s test | same, block-window |
|---|---|---|---|---|---|---|
| parser.py | 163 / 10.6 | 236 / 13.4 | 168 / 10.8 | 414 / 17.0 | 10 | 0 |
| tokenizer_core.py | 656 / 20.3 | 771 / 23.1 | 672 / 20.8 | 1011 / 30.0 | 10 | 1 |
| expressions/core.py | 314 / 18.6 | 386 / 20.3 | 320 / 18.7 | 475 / 22.8 | 7 | 0 |
| qualify_columns.py | 67 / 13.2 | 89 / 15.8 | 69 / 13.5 | 189 / 24.1 | 11 | 0 |
| simplify.py | 24 / 16.6 | 35 / 19.8 | 25 / 17.0 | 66 / 21.7 | 13 | 1 |
| scope.py | 223 / 21.6 | 262 / 23.2 | 229 / 21.8 | 365 / 25.7 | 6 | 0 |

- Function-level selection would be 1.5–11 s worse per edit than line-level.
- Block-window keeps almost all of the window's safety margin, because the whole multi-line statement is kept as one unit. It removes the slow-test leak almost completely.
- The existing slow-test prune (selector.py:505-512) covers only same-indent one-for-one replacements. Insertions, deletions and re-indents still leak.

### F5. Line events do not tell you which branch ran inside a multi-line expression (CPython 3.13)
In 2haz (dialect.py:1277-1281, `unsafe = (str.islower if … else str.isupper)`):
- line 1278 (`str.islower`, the true arm) has 86 tests;
- line 1280 (`else str.isupper`) also has 86 tests;
- line 1279 (the condition) has 22;
- line 1277 records nothing.

So coverage of continuation lines cannot tell which arm ran. **Branch-sensitive reasoning has to work at the level of statement blocks** (an `if`/`elif`/`else` body, a loop body, a `try` handler), never at the level of sub-expression lines. The coverage database has `has_arcs=0`, so the map holds no arc data.

### F6. A dead line falls back to the whole function, which is expensive
When nothing in the window ran, selector.py:520-522 takes the whole function's coverage. For statements nobody executed:

| file | such lines | whole-fn fallback | coverage of nearest executed enclosing header | same, minus ≥5 s tests |
|---|---|---|---|---|
| scope.py | 28 | 21.6 s (82 % include a slow test) | 0.0 | 0.0 |
| qualify_columns.py | 32 | 24.8 s (96 %) | 9.0 | 4.5 |
| simplify.py | 34 | 10.0 s (38 %) | 0.7 | 0.2 |
| helper.py | 25 | 9.7 s (36 %) | 0.0 | 0.0 |
| query.py | 35 | 6.6 s | 0.0 | 0.0 |
| parser.py | 23 | 6.0 s | 0.3 | 0.3 |

A header cost of 0 means no enclosing header ran either: the code is unreachable in the suite, for example after an early `return`.

### F7. Slow-test redundancy: what each slow test covers alone and what cheap tests also cover
Over every sqlglot line the map gives each test (tests ≥ 2 s):

| test | s | lines | covered by it alone | peers are all ≥2 s | ≥1 cheap test also covers | ≥10 cheap tests also cover | where the alone / only-slow lines are |
|---|---|---|---|---|---|---|---|
| test_max_nodes | 28.8 | 867 | 0 | 0 | 867 | 864 | none |
| test_execute_tpcds | 21.5 | 8899 | 1004 | 21 | 7874 | 6579 | planner 288, executor/python 204, env 191, table 103 (whole-file proxy) |
| test_generator_memory | 6.7 | 286 | 0 | 0 | 286 | 248 | none |
| test_execute_tpch | 3.9 | 1769 | 0 | 0 | 1769 | 1769 | none |
| test_optimized_tpch | 3.5 | 2509 | 0 | 0 | 2509 | 2490 | none |
| test_scope | 2.5 | 4065 | 4 | 0 | 4061 | 3999 | scope.py 4 |
| opt test_tpcds | 2.2 | 6699 | 0 | 21 | 6678 | 5936 | simplify 12, canonicalize 5 |

How to read this:
- **By coverage, every slow test except tpcds (on executor and planner) is fully redundant.** max_nodes and generator_memory never cover a line that at least one cheap test doesn't also cover.
- **But redundancy does not make it safe to exclude a test under this scorer.** A commit is a miss if *any* failing test goes unselected (SCORING.md "Misses"). If tpcds fails along with 20 cheap tests, selecting the cheap ones does not save the commit. In the sample, tpcds failed together with opt test_tpcds on both 2jrq and 3utz. Classic test-suite minimisation ("drop T if others cover its lines") is therefore unsound here.
- What redundancy does show is that a slow test's coverage of a line is **incidental**: the test's assertions are coarser than those of the cheap tests on the same line.
  - max_nodes asserts only "is a Select", "the limit error fires" and "reset works" (round-1 report 11 F1).
  - generator_memory asserts memory growth below 1 KB.
  - Such a test fails on an incidental line only if the edit **raises** on its inputs. An AST-valid edit can raise at runtime (AttributeError, wrong type), and hidden mutations do this. So these tests cannot be dropped on hot lines, where P(fail) ≈ 15–30 % > break-even of 6–10 %.
- The cases where dropping is sound are the ones where the slow test **provably does not run the changed code**: block-window (F4), dead-block (F6), the substring bug (F3), and the data-flow rule for attributes (F8). In each of these the slow test is selected only through coverage of *neighbouring* code.

### F8. Class-attribute edits: read the attribute's readers, not the whole class
4d63's attribute `NUMERIC_ESCAPES` is read in these places:
- tokens.py:606 (forwarded as `numeric_escapes=` into `TokenizerCore`);
- tokenizer_core.py:657, 1219, 1240, 1323/1362;
- generator.py:1723, 3283.

Coverage of the value-sensitive readers (skipping the forwarding line), intersected with the snowflake scope, is **151 tests, 8.6 s**, or 11.9 s if generator_memory is kept.

For core flags, I took each bool class attribute and the coverage of every line that reads `.NAME` (static grep):

| class | flags | mean cost | median cost | readers include max_nodes / tpcds |
|---|---|---|---|---|
| Generator | 95 | 10.2 s | 4.1 s | 1 / 27 flags |
| Parser | 40 | 16.3 s | 6.8 s | 7 / 18 flags |
| Tokenizer | 8 | 36.8 s | 36.8 s | all of them |

Today every one of these costs 27.6–36.8 s. Tokenizer flags are read only in `__init__`, so you need the kwarg hop (`X=self.NAME` → `self.x`) to gain anything there.

## (b) Savings on the sample, and recall risk on hidden commits

| change | sample saving | where | hidden-set expectation | patch shapes that would cause a miss |
|---|---|---|---|---|
| C1 path-aware fixture match | **−14.8 s** (4d63: 27.5 → 12.7) | name_tests, fixture_change | −15 to −21 s for each class-body edit to one of the 6 big dialects, −6 to −9 s for the next 7; plus fixture edits to `optimizer.sql`/`qualify_columns.sql` (round-1 report 11 F9) | a test that builds a fixture path from a directory name only and globs it (none exist today); a fixture referenced by a string > 200 chars (dropped by build_map.py:162) |
| C2 attribute-reader rule | ≈ −0.8 s on 4d63 after C1 (−4.2 s if generator_memory is also dropped) | class-body Assign in a nested or core class | 20–30 s per core-flag edit (36.8 → ~5–10 s) | the attribute read via `getattr(self, "NAME")` or `vars()`; a table processed by `__init_subclass__` or a metaclass (import-time, so keep the import guard); a subclass reading the attribute under another name |
| C3 block-scoped window | ≈ 0 (2haz still needs 1278/1280 → tpcds; 2jrq, 3utz and 66ks really need tpcds) | `fn` branch | 0.2–2.8 s on average per core-function edit, and it avoids the 20-plus s cases on 4–13 % of lines | a coverage attribution off by one into a sibling block: CPython puts a block's first instruction on the line of the `if`, which is the same block as the header, so that case is kept |
| C4 dead-block header rule | ≈ 0 | whole-fn fallback | 6–25 s for each edit to an untested branch of optimizer, helper or scope code | an edit in a branch reached only from a spawned pool (executor and tpcds children): excluded by a file guard; a cache-gated block (`_build_dispatch`): excluded through `import_fns` |

## (c) Code changes

### C1. `selector.py`: path-aware fixture stem matching (two sites)
```python
# module level, next to HUNK
_FX_PAT: dict[str, re.Pattern] = {}
def fixture_ref(stem: str) -> re.Pattern:
    """`stem` as a file name inside a string: not part of a longer name ("qualify_columns__invalid")
    and not a directory ("/optimizer/tpc-ds/")."""
    p = _FX_PAT.get(stem)
    if p is None:
        p = _FX_PAT[stem] = re.compile(r"(?<![\w-])" + re.escape(stem) + r"(?![\w/-])")
    return p
```
```diff
@@ def fixture_tests(self, fixture_rel: str) -> set[int]:
-            memo[stem] = {n for n, strings in self.strings.items() if any(stem in s for s in strings)}
+            pat = fixture_ref(stem)
+            memo[stem] = {n for n, strings in self.strings.items() if any(pat.search(s) for s in strings)}
@@ def fixture_change(self, f: dict):
-        hits = {n for n, strings in self.strings.items() if any(stem in s for s in strings)}
+        pat = fixture_ref(stem)
+        hits = {n for n, strings in self.strings.items() if any(pat.search(s) for s in strings)}
```
Leave the `fixture-dir` fallback (substring on the parent directory) unchanged. It only fires when the stem matches nothing.

### C2. `tools/build_index.py` and `selector.py`: readers of class attributes
In build_index.py, add per UPPER_CASE name the lines that read it (`Attribute.attr == NAME`). Follow one forwarding hop: when the read is the value of a keyword argument `kw=….NAME`, record reads of `.kw` instead of the forwarding line. A string `getattr(x, "NAME")` records `"*"` (unknown).
```python
attr_reads: dict[str, list] = defaultdict(list)
for rel, tree in trees.items():                      # every sqlglot/*.py already parsed here
    fwd = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and isinstance(node.value, ast.Attribute) and node.arg:
            fwd[id(node.value)] = node.arg
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr.isupper() and isinstance(node.ctx, ast.Load):
            kw = fwd.get(id(node))
            attr_reads[node.attr].append([rel, node.lineno] if kw is None else ["->", kw])
        elif (isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("getattr", "hasattr")
              and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)):
            attr_reads[str(node.args[1].value)].append(["*", 0])
# resolve "->kw": reads of `.kw` (lower-case attribute) anywhere
lower_reads = defaultdict(list)
for rel, tree in trees.items():
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr.islower():
            lower_reads[node.attr].append([rel, node.lineno])
index["attr_reads"] = {n: sorted({tuple(x) for r in v for x in ([r] if r[0] != "->" else lower_reads.get(r[1], [["*", 0]]))})
                       for n, v in attr_reads.items()}
```
In selector.py, inside the module/class-body branch of `sqlglot_change`, before `import_time_change`:
```python
def attr_edit(self, rel, old_tree, new_tree, ln):
    """Changed `NAME = …` in a class body whose new value adds no Name/Attribute: tests reading NAME."""
    old_st, new_st = stmt_at(old_tree, ln), matching_stmt(new_tree, old_tree, ln)  # same class path & target
    if not (isinstance(old_st, (ast.Assign, ast.AnnAssign)) and new_st is not None): return None
    tgt = target_name(old_st)
    if not tgt or not tgt.isupper() or tgt != target_name(new_st): return None
    names = lambda s: {ast.dump(n) for n in ast.walk(s.value) if isinstance(n, (ast.Name, ast.Attribute))}
    if not names(new_st) <= names(old_st): return None          # new references could raise at import
    reads = self.index.get("attr_reads", {}).get(tgt)
    if not reads or any(r[0] == "*" for r in reads): return None
    if any(self.in_import_fn(r, l) for r, l in reads): return None  # consumed at class creation
    hit = set()
    for r, l in reads:
        hit |= self.cov(r, range(l, l + 1))
    if rel.startswith(DIALECT_DIRS):
        family = self.dialect_family(rel)
        scope = set().union(*(self.file_cov(f) for f in family))
        scope |= self.name_tests({Path(f).stem for f in family} - {"__init__", "base"})
        hit &= scope
    return hit or None
```
Use it with `hit = self.attr_edit(...)`, falling back to `import_time_change` when it returns None. Keep the `lost`/`new_names` checks that follow unchanged. Keep `all_dialect_tests` (generator_memory) for dialect files unless the target is never read in an `import_fns` range, which the code above already requires. That is the decision behind the extra −3.4 s on 4d63: I'd keep generator_memory at first.

### C3. `selector.py`: limit the window to the changed line's block
```python
def block_ids(tree) -> dict[int, int]:
    """Line -> id of the innermost statement list holding it (a whole statement shares one id)."""
    out: dict[int, int] = {}
    def visit(node):
        for fld in ("body", "orelse", "finalbody", "handlers", "cases"):
            b = getattr(node, fld, None)
            if not isinstance(b, list):
                continue
            for s in b:
                if hasattr(s, "lineno"):
                    for x in range(s.lineno, (s.end_lineno or s.lineno) + 1):
                        out[x] = id(b)
                visit(s)
    if tree is not None:
        visit(tree)
    return out
```
```diff
@@ def sqlglot_change(self, f, old_tree, new_tree):
+        blk = block_ids(old_tree)
+        table = self.files.get(rel, {}).get("lines", {})
@@             elif fn:
                 lo, hi = max(fn[0], ln - WINDOW), min(fn[1], ln + WINDOW)
-                hit = self.cov(rel, range(lo, hi + 1))
+                window = range(lo, hi + 1)
+                if ln in blk and str(ln) in table and self.test_set(table[str(ln)]):
+                    # A recorded line: neighbours in other blocks (the other arm of an if, the code
+                    # after a loop) run under different tests. Nested lines' tests are a subset of ln's.
+                    window = [x for x in window if blk.get(x) == blk[ln]]
+                hit = self.cov(rel, window)
```
Insertions are unaffected: `touched_old_lines` already adds both neighbours as changed lines of their own.

### C4. `selector.py`: dead block → nearest executed header, not the whole function
```diff
                 if not hit:
-                    # Nothing near the change ran: take the whole function, then the class.
-                    hit = self.cov(rel, range(fn[0], fn[1] + 1))
+                    hdr = None
+                    if not rel.startswith(("sqlglot/executor/", "sqlglot/planner.py")):
+                        hdr = executed_header(old_tree, ln, table, self)   # walk parents up to the def
+                    if hdr is not None:
+                        # No test ran this block; tests that evaluated its guard are insurance against
+                        # misattributed coverage. Slow tests measured in-process can't have run it either.
+                        hit = self.cov(rel, [hdr]) - (self.slow - TPC_PROXIED if rel.startswith("sqlglot/optimizer/") else self.slow)
+                    else:
+                        hit = self.cov(rel, range(fn[0], fn[1] + 1))
```
`executed_header` returns the line of the innermost enclosing `if`/`for`/`while`/`try`/`with`/`match` whose line is recorded with a non-empty set. It returns None when the walk reaches the def without finding one, and None when `self.in_import_fn` holds. `TPC_PROXIED` = {test_execute_tpcds}: its optimizer coverage is a proxy, so it stays. If the statement sits directly in the function body after a `return`, None keeps today's behaviour. A later step could return ∅ there.

## (d) Ranked recommendations

1. **C1 path-aware fixture match.** −14.8 s on the sample (4d63), and −15 to −21 s per hidden class-body edit to a big dialect. **Risk: very low.** No test globs a fixture directory, and the loaders use explicit `dir/name.sql` paths. It also fixes round-1 report 11's F9 at `fixture_change`.
2. **C3 block-scoped window.** About 0 s on the sample, 0.2–2.8 s per hidden core-function edit, and slow-test leakage drops from 4–13 % of lines to 0–1 %. **Risk: low.** A multi-line statement stays one unit (F5), and unrecorded lines keep the full window.
3. **C2 attribute-reader rule.** About −0.8 s on the sample. On a hidden core-flag edit it saves 20–30 s (36.8 → median 4–7 s). **Risk: medium-low.** It is guarded by `getattr` → None, readers in `import_fns` → None, and new Names in the RHS → None. Start with Generator and Parser flags plus dialect nested classes, and leave Tokenizer as is.
4. **C4 dead-block header rule.** About 0 s on the sample, 6–25 s for each edit to an untested optimizer, helper or scope branch. Hidden mutation-style commits that "fail nothing" often land in such branches. **Risk: medium-low** with the executor/planner and import_fns guards.
5. **Do not** drop max_nodes, generator_memory or tpcds because cheap tests also cover the same line (F7). The scorer counts a commit as missed if any failing test is left out, and these tests fail on any runtime crash along their path. Use redundancy only as a diagnostic.
6. Don't try line-level branch inference inside multi-line expressions (F5). Record arcs (`--cov-branch`) at the next map rebuild if real branch sensitivity is wanted. The current `.coverage` has `has_arcs=0`.

Summary:
1. On core files the cost comes from tpcds and max_nodes being selected, not from the test count. Median tests per parser line is 11, and tpcds is on 19–77 % of core lines, mostly measured for real.
2. 4d63's 561 tests (27.5 s) come from a substring bug in `fixture_tests` ("optimizer" matches "/optimizer/tpc-ds/"). Path-aware matching cuts it to 12.7 s (−14.8 s) and fixes 13 dialects.
3. Limiting the ±2 window to the changed line's block cuts slow-test leakage from 4–13 % of lines to 0–1 %. Branch reasoning has to use AST blocks: CPython 3.13 line events do not separate the arms of a multi-line ternary.
4. Core class-body flags cost 27–37 s today. A reader-coverage rule brings Generator and Parser flags to a median of 4–7 s, and dead branches should use their guard's coverage instead of the whole function's.
5. The redundancy table shows max_nodes and generator_memory cover no line alone, but a miss counts per failing test, so they cannot be dropped for redundancy. Only "provably didn't run it" cuts are safe.
