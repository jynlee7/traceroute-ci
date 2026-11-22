# 11: The cost floor set by slow tests

Scope: every test over 1 s in `sample/tests.json`. For each one: what it asserts, which changes can make it fail (not just run it), the expected-value threshold, and a narrowing rule with its risk.
Method: I read the test sources under `work/sqlglot/tests`, the coverage map (`maps/impact.json.gz`, read-only one-liners) and `selector.py`/`tools/build_map.py`. I ran nothing.

## 0. What dropping a slow test is worth (exact `split()` cost model)

Marginal cost = cost(selection + T) − cost(selection). I computed it with the scorer's rule (`max load + 2 s × k`, best k ≤ 8) for a fast-test load L that can be divided freely. Break-even = marginal / 300 s.

| T (s) | L=2 | L=5 | L=10 | L=20 | L=40 | break-even P(fail) |
|---|---|---|---|---|---|---|
| test_max_nodes 28.8 | +28.8 | +26.3 | +23.8 | +20.1 | +16.8 | **5.6 – 9.6 %** |
| test_execute_tpcds 21.5 | +21.5 | +19.0 | +16.5 | +12.8 | +9.5 | **3.2 – 7.2 %** |
| tpcds when max_nodes is already in | +0.0 | +0.0 | +1.4 | +2.0 | +1.3 | **< 0.7 %** (nearly free) |
| test_generator_memory 6.7 | +6.7 | +4.2 | +3.3 | +2.0 | +1.3 | 0.4 – 2.2 % |
| test_execute_tpch 3.9 / test_optimized_tpch 3.5 | +3.9 | +1.9 | +1.6 | +1.3 | +0.8 | 0.3 – 1.3 % |
| 1–2.5 s tests (scope, opt tpcds, annotate_funcs, …) | ≤2.5 | ≤1.2 | ≤1.2 | ≤0.8 | ≤0.5 | ≤ 0.8 % |

Conclusions:
- Only max_nodes and tpcds are worth a dedicated gate.
- generator_memory is worth gating only where the evidence is structural.
- For every test under 4 s the only safe cut is P(fail) ≈ 0 by construction, such as a proxy that is plainly wrong.
- The decisions are **coupled**: once one ≥20 s test is in, a second one costs about 0–2 s. Gate the slow tests jointly by marginal cost, not one at a time (snippet C4).

## (a) Findings

**F1. test_max_nodes (test_parser.py:1567-1591) asserts only these six things:**
1. `parse_one("SELECT 1", max_nodes=1e6)` is an `exp.Select`.
2. The same holds for 500 plain columns `FROM t`.
3. `SELECT x=x=…(100 deep) ×10 000` raises `ParseError` containing `"Maximum number of AST nodes"`.
4. At `max_nodes=50_000` the message contains `"(50000)"`.
5. `SELECT x=0,…,x=99` with `max_nodes=5` raises.
6. A plain parse afterwards succeeds, so the counter resets.

- The counting and raising live in `Parser.validate_expression` (parser.py:2199-2206, `_node_count` at 2201-2203) and `raise_error` (parser.py:2183-2197 → `errors.py:58-68 ParseError.new`, `errors.py:104 highlight_sql`), plus `reset()` (parser.py:2062-2075).
- Its coverage is 867 lines. Most of that is the generic default-dialect path:
  - parser.py: 512 lines, mostly `_parse_select_query`, `_parse_column_parts_fast`, `_parse_table_parts_fast`, `_parse_equality`, `_parse_atom` and similar;
  - tokenizer_core.py: 210;
  - expressions/core.py: 46;
  - tokens.py: 38 (`_init_core`);
  - dialect.py: 18 (`get_or_raise`, `parser`, `tokenize`).
- The median coverage-set size on those lines is 1283 tests. Only 3 parser lines (the node-count branch) have sets under 50.
- So it fails only when a change does one of these:
  - (i) crashes, or raises a non-limit `ParseError`, on those four trivial inputs;
  - (ii) changes what `validate_expression` counts or raises, or the message text;
  - (iii) breaks `reset`;
  - (iv) slows the hot path enough to hit a pytest timeout. `pytest-timeout==2.4.0` is in `requirements.lock`, and the scorer counts a timeout as a failure.
- Mutations that only change the *shape* of the AST (a wrong arg key, quoted flags, an alternate fast path) still return a `Select` with about the same node count, so it passes.
- My estimate:
  - P(fail | an executed line on its path is mutated) ≈ 15–30 %, which is above break-even, so keep it.
  - P(fail | the changed line is *not* executed by it) ≈ 0.
  - P(fail | a class-table edit to an attribute it never reads) ≈ 0, apart from import-time crashes (F7).

**F2. test_execute_tpch (test_executor.py:187-188) executes nothing.**
- `mp_execute` returns `None` unless `meta["execute"]` (test_executor.py:56-58).
- `tests/fixtures/optimizer/tpc-h/tpc-h.sql` has **zero** `# execute:` lines. tpc-ds has 57 out of 99 queries.
- So the test only:
  - runs `parse_one` on the 22 TPC-H inputs in the parent (test_executor.py:180);
  - pickles each expression through `Expr.__reduce__` → `serde.dump` (expressions/core.py:890-898), then `serde.load` in the spawned child;
  - depends on `setUpClass` (test_executor.py:70-121), which builds `Table` objects and loads fixtures.
- But `tools/build_map.py:141-153` gives it the **whole optimizer proxy** (the coverage of test_optimized_tpch, test_optimizer::test_tpch and test_tpcds) plus **all of `sqlglot/executor/` and `planner.py`**. That is 8735 lines, against about 2.5k lines of real path. Pure over-selection.
- Example: an edit to executor/context.py (`66ks`) or optimizer/eliminate_joins.py (`2jrq`) hits tpch *exactly* in the map (my coverage check), yet tpch cannot fail on either.

**F3. test_execute_tpcds (test_executor.py:190-191, helpers 165-185) compares *query results*, not SQL text.**
- On one side: `execute()` = `optimize(leave_tables_isolated=True)` → `Plan` → `PythonExecutor` (executor/__init__.py:76-86), run in a spawned child.
- On the other side: duckdb running `transpile(sql, write="duckdb")` (`cached_execute`, test_executor.py:123-127), in the parent.
- It fails on:
  - an optimizer, planner or executor change that alters semantics;
  - a parser change on TPC-DS constructs;
  - a duckdb-generator change that alters semantics;
  - a crash anywhere, including `expression.sql(pretty=True)`, which is evaluated eagerly in `logger.debug` (executor/__init__.py:79);
  - a serde change.
- It does **not** fail on edits to the *expected* (second) statement of each fixture pair: `for args, sql, _ in sqls` (test_executor.py:180) and `_, sql, _ =` (test_executor.py:168).
- No `.coverage.exec` exists. `work/pytest_exec.log` has no result line, so `tools/coverage_executor.sh` never finished. The child side is therefore proxy-only, while the parent side (parse, `dump`, duckdb transpile) is measured in-process. The whole-executor proxy is right for tpcds.

**F4. test_optimized_tpch (test_executor.py:157-163)** compares duckdb results of `transpile(sql→duckdb)` with `transpile(optimized→duckdb)`.
- It never calls the optimizer. It depends on the parser, the default→duckdb generator for TPC-H constructs, and both halves of tpc-h.sql.
- It is measured in-process and has no proxy inflation of its own. No change is needed.

**F5. test_generator_memory (test_generator.py:19-38)**, for every `Dialects` member: `get_or_raise`, then 100 + 1000 `dialect.generator()` calls, then asserts the traced memory growth is below 1024 B.
- It fails only on these:
  - (i) per-instance state that leaks into module- or class-level storage: `Generator.__init__` (generator.py:~955-1006), `_build_dispatch` / `_DISPATCH_CACHE` (generator.py:71-89), `Dialect.generator` (dialect.py:1378-1380), `get_or_raise` (dialect.py:1110-1146), subclass `__init__` overrides (generators/athena.py:154-177, bigquery.py:719-739), `helper.name_sequence` (helper.py:279);
  - (ii) any dialect failing to load or construct, e.g. an import-time crash in *any* dialect module (F7).
- Generator class tables (`TRANSFORMS`, flags) and every `*_sql` body are irrelevant to it.
- Its map coverage also contains import-time noise: `Dialect.__new__` (dialect.py:307-432), `Tokenizer.__init_subclass__` (tokens.py:85-124), and all of the dax modules. These come from xdist import order, not from its assertions.

**F6. The other tests over 1 s** are all fixture or logic tests whose coverage is measured. Durations:
- test_optimizer: test_scope 2.5, test_tpcds 2.2, test_annotate_funcs 2.0, test_qualify_columns 1.4, test_optimize_in_nulls 1.3, test_merge_subqueries 1.0, test_canonicalize 1.0;
- test_executor::test_agg_order 1.4;
- test_schema::test_cache_does_not_retain_queries 1.3.

What each asserts:
- test_scope (test_optimizer.py:3388) asserts that `build_scope().traverse()` equals `traverse_scope` over a 10 000-way UNION. Its cost is recursion and scale; it fails on scope traversal changes.
- test_cache_does_not_retain_queries (test_schema.py:406) asserts that MappingSchema caches hold no strong references, through weakref plus gc.
- test_agg_order (test_executor.py:1720) asserts the alias order of `Plan` aggregations.

Their marginal cost is ≤ 2.5 s, so break-even is ≤ 0.8 %. Exact coverage is the right gate, so no narrowing.

**F7. An import-time crash fails everything that imports the module, including all slow tests.**
- The class-body path (`import_time_change`, selector.py:250-261) takes the coverage of the dialect family plus name and class tests. For a core module (parser.py, tokens.py, generator.py) that is most of the suite but not all of it.
- Any narrowing of class-body edits must keep a guard for edits that can raise at import, such as `exp.Missing` or an unknown `TokenType.X`.

**F8. The comment at selector.py:30 says "Slow tests are kept only when their coverage hits the changed lines themselves", but no code implements it.**
Today slow tests also come in through:
- the ±2 WINDOW (selector.py:301-302);
- the whole-function and whole-class fallback (selector.py:303-307);
- `file_cov` of the whole family on any class-body edit (selector.py:254-255);
- `class_tests` (selector.py:258-260);
- `new_method_tests` (selector.py:327-332).

**F9. The fixture substring rule over-selects slow tests.**
- `fixture_change` (selector.py:379) uses `stem in s`.
- For `tests/fixtures/optimizer/optimizer.sql`, the stem `optimizer` is a substring of test_executor's module constants `"/optimizer/tpc-ds/"` and `"/optimizer/tpc-h/"`. Those constants are folded into every test of the file (build_map.py:51-65).
- Result: **tpcds, tpch and otpch are selected for an edit to optimizer.sql**, which they never read. That is about 21.5 s of waste on that shape. `jsonpath/cts.json` also pulls in test_scope and test_tpcds.

## (b) Recall risks and cost waste under the current selector.py

Recall risks:
- **R1 (medium):** an AST-valid class-body edit that raises at import in a non-core dialect module: an unknown `exp.X`, a bad `TokenType.X`, or a wrong type consumed by `Tokenizer.__init_subclass__`. test_generator_memory loads *every* dialect, and test_time.py:26 / test_dialect.py:226,3875 loop over `Dialects`. The coverage of a family such as snowflake does not contain those tests (gm's coverage holds only dax/athena/bigquery files). → miss.
- **R2 (low):** a change to `sqlglot/executor/table.py` that breaks `setUpClass`. Every TestExecutor test then errors, but coverage gives the setup lines to whichever test ran first (build_map.py:103 strips the `|setup` phase). The executor proxy saves tpch and tpcds today. Keep that when fixing F2 (see C1).
- **R3 (low, timing):** a change that only slows the tokenizer or parser hot path, e.g. disabling the `_parse_*_fast` fast paths, changes no output but can time out max_nodes. Exact coverage still selects it. Don't add an "output-preserving ⇒ drop" rule for it.

Waste:
- W1, F2: tpch is selected on every optimizer, planner or executor edit. About +0.8–3.9 s per commit.
- W2, F8: max_nodes, tpcds and generator_memory on class-body edits of parser.py, tokens.py, generator.py or dialect.py whose attribute they never read. Up to +30 s per commit.
- W3, F9: tpcds, tpch and otpch on optimizer.sql edits. +21.5 s.
- W4: tpc-ds.sql or tpc-h.sql edits limited to *expected* halves still select execute_tpcds and execute_tpch. +19 s.

## (c) Proposed code

**C1. tools/build_map.py:141-153: give the execution proxy only to tests whose fixtures really execute.** This is derived from the fixture meta at the pin.
```diff
-    tpc_exec = {index[t] for t in ("tests/test_executor.py::TestExecutor::test_execute_tpch",
-                                    "tests/test_executor.py::TestExecutor::test_execute_tpcds") if t in index}
+    # mp_execute (tests/test_executor.py:56) returns None unless the fixture pair carries
+    # "# execute: true". Only those tests run optimize/plan/execute in the child; the others just parse
+    # and pickle (measured in-process, plus the pickle lines above).
+    fixture_of = {"tests/test_executor.py::TestExecutor::test_execute_tpch": "optimizer/tpc-h/tpc-h.sql",
+                  "tests/test_executor.py::TestExecutor::test_execute_tpcds": "optimizer/tpc-ds/tpc-ds.sql"}
+    executes = lambda f: "# execute: true" in (ROOT / "tests/fixtures" / f).read_text()
+    tpc_exec = {index[t] for t, f in fixture_of.items() if t in index and executes(f)}
+    setup_only = {index[t] for t, f in fixture_of.items() if t in index and not executes(f)}
@@
         for ln, sid in list(lines.items()):
             members = set(set_table[sid])
+            if rel == "sqlglot/executor/table.py":  # TestExecutor.setUpClass builds Table objects
+                members |= setup_only
             if whole or members & proxy_ids:
                 lines[ln] = intern(members | tpc_exec)
+            else:
+                lines[ln] = intern(members)
```
This needs a map rebuild (`python tools/build_map.py` on the existing `.coverage` data). Risk: **low**. tpch keeps its own parse and serde coverage, the serde lines (build_map.py:118-132) and table.py.

**C2. selector.py: put the slow tests on a strict, assertion-path gate (implements the comment at selector.py:30).** Add these to the constants and `Selector.__init__`:
```python
SLOW_S = 5.0   # tests this slow cost >= ~5 s at the margin (see research/11)
P_WEAK = 0.01  # assumed P(fail) of a slow test reached only through widening rules
# in __init__:
        self.ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
        self.slow = {t["number"] for t in tests if t["seconds"] >= SLOW_S}
        self.slow_reads = {self.by_id[k]: frozenset(v) for k, v in self.index.get("slow_reads", {}).items() if k in self.by_id}
        self.strict: set[int] = set()   # slow tests with direct evidence, reset per commit
```
In `sqlglot_change`, add the strict evidence next to the existing loop (selector.py:298-326). This is new code; the broad `out` is unchanged.
```python
        sig = signature_lines(old_tree)
        for ln in sorted(changed):
            fn, _cls = self.innermost(rel, ln)
            if fn:
                rng = sig.get(ln)  # def header / decorator / default args: runs at def time
                self.strict |= self.cov(rel, range(rng[0], rng[1] + 1) if rng else [ln]) & self.slow
            else:
                lam = self.lambda_range(rel, ln)
                lam_hit = self.cov(rel, range(lam[0], lam[1] + 1)) if lam else set()
                self.strict |= (lam_hit & self.slow) if lam_hit else self.slow_class_body(f, rel, ln, old_tree)
        for _cls, name in new_names:  # a new override only matters if a slow test calls that name here
            self.strict |= {n for n in self.slow & self.file_cov(rel) if name in self.slow_reads.get(n, {name})}
```
New helpers:
```python
    def slow_class_body(self, f, rel, ln, old_tree) -> set[int]:
        """Slow tests a class-table / module-level edit can reach: the edited name must be read
        on a line the test executes, in a file it executes. Unknown references can crash at import."""
        if import_crash_risk(f, self.index):
            return set(self.slow)
        names = assigned_names(old_tree, ln) | added_targets(f)
        reach = {n for fam in self.dialect_family(rel) for n in self.file_cov(fam) & self.slow}
        if not names:  # an expression statement, loop, call...: keep the conservative set
            return reach
        return {n for n in reach if n not in self.slow_reads or names & self.slow_reads[n]}

def signature_lines(tree) -> dict[int, tuple[int, int]]:
    out = {}
    for n in ast.walk(tree) if tree else ():
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.body:
            start = min([n.lineno] + [d.lineno for d in n.decorator_list])
            for ln in range(start, n.body[0].lineno):
                out[ln] = (start, n.end_lineno)
    return out

def assigned_names(tree, ln) -> set[str]:
    for n in ast.walk(tree) if tree else ():
        if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and n.lineno <= ln <= n.end_lineno:
            tg = n.targets if isinstance(n, ast.Assign) else [n.target]
            return {x.id if isinstance(x, ast.Name) else x.attr for t in tg for x in ast.walk(t) if isinstance(x, (ast.Name, ast.Attribute))}
    return set()

ASSIGN = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?::[^=]+)?[+|&-]?=")
def added_targets(f) -> set[str]:
    return {m.group(1) for h in f["hunks"] for tag, text, _ in h["lines"] if tag == "+" and (m := ASSIGN.match(text))}

def import_crash_risk(f, index) -> bool:
    """An added exp.X / TokenType.X that does not exist at the pin would raise at import."""
    known_exp, known_tok = set(index.get("exp_names", ())), set(index.get("token_types", ()))
    if not known_exp or not known_tok:
        return True  # no index: assume the worst
    for h in f["hunks"]:
        for tag, text, _ in h["lines"]:
            if tag == "+":
                if any(x not in known_exp for x in re.findall(r"\bexp\.([A-Za-z_]\w*)", text)): return True
                if any(x not in known_tok for x in re.findall(r"\bTokenType\.([A-Z_][A-Z0-9_]*)", text)): return True
    return False
```
Notes on `import_crash_risk`:
- A name the same patch defines would read as unknown and keep everything. That errs toward selecting (safe).
- It also closes R1 if, in `import_time_change`, `import_crash_risk` returns `set(self.all)`. Recommended.

**C3. tools/build_index.py: add `slow_reads`, `exp_names` and `token_types`.**
- These are built from the pin plus the committed map, in `docker build`, where CPU is not charged.
- `maps/slow_ids.json` would list the ids of the tests ≥ 5 s in `sample/tests.json`. That is an input duration, not an answer. At runtime, a slow test with no entry is simply not gated (conservative).
```python
def slow_reads(root: Path, map_path: Path, ids: list[str]) -> dict[str, list[str]]:
    """Identifiers on every line each slow test executes, widened to whole simple statements and
    compound headers (coverage records a multi-line statement on its first line only)."""
    m = json.load(gzip.open(map_path, "rt"))
    want = {i: tid for i, tid in enumerate(m["tests"]) if tid in set(ids)}
    member = [set(s) & want.keys() for s in m["sets"]]
    words = {i: set() for i in want}
    ident = re.compile(r"[A-Za-z_]\w*")
    for rel, d in m["files"].items():
        p = root / rel
        if not p.is_file():
            continue
        src = p.read_text().split("\n"); span = {}
        for n in ast.walk(ast.parse("\n".join(src))):
            if isinstance(n, ast.stmt):
                end = n.body[0].lineno - 1 if hasattr(n, "body") and n.body else n.end_lineno
                span.setdefault(n.lineno, max(n.lineno, end))
        for ln, sid in d["lines"].items():
            if member[sid]:
                a = int(ln); text = " ".join(src[a - 1: span.get(a, a)])
                for i in member[sid]:
                    words[i].update(ident.findall(text))
    return {want[i]: sorted(w) for i, w in words.items()}
# exp_names: every top-level def/class name in sqlglot/expressions/*.py
# token_types: targets assigned in the body of `class TokenType` (tokenizer_core.py:15)
```
- With these, a `Parser.PROPERTY_PARSERS` or `TYPE_TOKENS` edit drops max_nodes, because neither name is read on its covered lines.
- A `Parser.EQUALITY`, `STATEMENT_PARSERS` or `Tokenizer.SINGLE_TOKENS` edit keeps it. This is coarse but sound: an attribute a test never reads cannot change its outcome.

**C4. selector.py `select()`: gate jointly by marginal cost.** Put this after the file loop (selector.py:403-405). Keep every non-`sqlglot:` reason ungated with `self.strict |= s & self.slow`.
```python
        weak = (out & self.slow) - self.strict
        out -= weak
        for n in sorted(weak, key=lambda n: self.ms[n]):  # cheapest first: they may ride free
            if cost_ms(out | {n}, self.ms) - cost_ms(out, self.ms) <= 300_000 * P_WEAK:
                out.add(n)
        self.strict = set()

def cost_ms(sel, ms) -> int:
    w = split(sel, ms)
    return max((sum(ms[n] for n in b) for b in w), default=0) + WORKER_MS * len(w)
```
Example: when max_nodes is kept with direct evidence, a weak tpcds rides along at no cost (marginal 0–2 s ≤ 3 s).

**C5. selector.py `fixture_change`: exact fixture names first, and pair halves for input-only tests.**
```python
        rel_fx = str(path).split("tests/fixtures/", 1)[-1]          # e.g. optimizer/tpc-ds/tpc-ds.sql
        exact = lambda s: s in (stem, rel_fx, rel_fx[:-4]) or s.endswith(("/" + stem, "/" + path.name))
        hits = {n for n, strings in self.strings.items() if any(exact(s) for s in strings)}
        if hits:
            if path.suffix == ".sql" and not pair_inputs_touched(self.source(f["old"]) or "", f):
                hits -= self.input_only   # tests that bind the expected slot to "_" (test_executor.py:168,180)
            return hits, "fixture-exact"
        # ...then fall back to the current substring rules unchanged
```
```python
def pair_inputs_touched(old: str, f) -> bool:
    """True if any edit lands in an input (even) statement, a "#" meta line, or adds or removes a ';'
    (which re-pairs everything). Mirrors tests/helpers.py: drop empty/"--" lines, split on ';'."""
    changed = [t for h in f["hunks"] for tag, t, _ in h["lines"] if tag in "+-"]
    if any(";" in t or t.startswith("#") for t in changed):
        return True
    semis, stmt_of = 0, {}
    for no, line in enumerate(old.split("\n"), 1):
        stmt_of[no] = semis
        if line and not line.startswith("--"):
            semis += line.count(";")
    return any(stmt_of.get(ln, 0) % 2 == 0 for ln in touched_old_lines(f))
```
- `self.input_only` is derived statically in build_index. It holds the tests whose method, and the `self.*` helpers they call, unpack fixture triples only as `(…, …, _)`. At the pin that is execute_tpch and execute_tpcds; test_optimized_tpch uses `optimized`.
- Risk: **low**. An input edit or any `;`/meta change keeps everything.

## (d) Ranked recommendations

| # | Change | Expected impact | Recall risk |
|---|---|---|---|
| 1 | **C2 + C4**, the strict gate for the ≥5 s tests with joint marginal re-add (exact changed lines, def-header → whole function, no WINDOW or fallback expansion) | Removes max_nodes, tpcds and generator_memory reached only by widening. About 15–28 s saved on each such commit. In the sample, the WINDOW/function path added no slow test, so the bigger win is class-body edits (below). | Low. A non-executed line cannot change the run, and signature lines are handled. |
| 2 | **C3 + class-body branch of C2**: keep a slow test on a class-table edit only if it reads the edited name, in a file it executes | Up to about 30 s per class-body edit of parser.py, tokens.py, generator.py or dialect.py (today these pull in all slow tests through `file_cov`) | Low–medium. Dynamic `getattr(cls, f"…")` reads are invisible; the guard is that names in string constants are also collected. |
| 3 | **`import_crash_risk` guard** in the class-body paths, returning all tests when it fires | Closes R1, the missed `Dialects`-loop tests. It rarely fires, so the cost is small. | Lowers risk. |
| 4 | **C1**: tpch loses the optimizer/executor proxy (its fixture has no `# execute:`) | 0.8–3.9 s on every optimizer, planner or executor commit (about 10 of 30 sample commits touch those areas) | Low, with table.py kept for setUpClass. |
| 5 | **C5**: exact fixture matching plus input/expected halves | 19–21 s on optimizer.sql edits, and on tpc-* edits that touch only expected halves | Low, because it falls back to the substring rule. |
| 6 | Re-run `tools/coverage_executor.sh` successfully (its log has no result) and narrow the tpcds proxy to the measured child coverage | Some tpcds cuts on optimizer rules the TPC-DS run skips under `leave_tables_isolated=True` | Medium until measured. Keep the whole-executor proxy until then. |
| 7 | No gate for tests under 4 s (otpch, scope, opt tpcds, annotate_funcs, …) beyond exact coverage | — | Break-even P ≤ 1.3 %, which no general rule can show. |

Rules **not** to add:
- "Drop max_nodes for output-preserving or performance-only changes": timeout risk, R3.
- "Drop tpcds because test_optimizer::test_tpcds covers it": both fail together, so dropping either is a miss.
