# r2/01: When can test_execute_tpcds (21.5 s), test_execute_tpch and test_optimized_tpch fail?

Method: I read `work/sqlglot/tests/test_executor.py`, `tests/test_optimizer.py`, `tests/helpers.py`, `sqlglot/executor/*`, `sqlglot/serde.py`, `selector.py` and `tools/build_map.py`. I inspected `maps/impact.json.gz` and the raw `work/sqlglot/.coverage` (read-only sqlite) with small one-liners. I ran nothing. I build on round-1 reports 07 and 11 rather than repeat them. Since then, the TPC-H proxy has been dropped and `generators/python.py` is filled whole (`build_map.py:136-157`).

## (a) Findings

### F1. What test_execute_tpcds actually runs (test_executor.py:165-191)
Parent process, where coverage is native (raw `.coverage` context `test_execute_tpcds|run` = 2566 lines):
- `parse_one` on all 99 TPC-DS inputs (`:181`).
- Pickling through `Expr.__reduce__` → `serde.dump` (core.py:890-898). This happens in the pool feeder thread and is measured: 36 serde lines are native.
- `transpile(sql, write="duckdb")` only for the 57 entries the child returns a Table for (`cached_execute` `:123-127`, `subtestHelper` `:165-173`). Generator: 605 lines, duckdb generator: 48.
- The `assert_frame_equal` comparison, which runs no sqlglot code.
- The **expected (second) statement of each pair is never read**: `for args, sql, _` (`:181`) and `_, sql, _ =` (`:168`).

Child process (spawn), where coverage is not measured, so the map uses a proxy:
- `serde.load/_load`.
- `find_tables`.
- `execute()` (executor/__init__.py:53-86):
  - `ensure_tables` (`Tables(AbstractMappingSchema)`) and `ensure_schema`, which builds column types through the parser.
  - `optimize(..., leave_tables_isolated=True)`.
  - `logger.debug("...%s", expression.sql(pretty=True))` at `:79`. Its argument is evaluated eagerly, so the default generator runs on every optimized query.
  - `Plan`, `PythonExecutor`, the `Python` dialect (generators/python.py) and `env.ENV`.
- The child returns its result Table by plain pickle. No sqlglot code runs to unpickle it.

### F2. test_optimizer::test_tpcds is the proxy, and the proxy's limits
- It runs `optimize(sql, schema=TPCDS_SCHEMA)` on the same 99 inputs and compares the pretty SQL with the expected text (test_optimizer.py:3363-3366).
- It passes `execute=False`, so it never runs duckdb. The executor test also uses `isolate_tables=True` (optimizer.py:103).
- The only kwarg that differs is `leave_tables_isolated`. It is read only at merge_subqueries.py:453. It changes which subqueries get merged, so later rules (pushdown_*, eliminate_subqueries, simplify) and the planner see different shapes. The resulting under-coverage is spread across many rules. Widening merge_subqueries.py would not fix it: with `True`, that file mostly takes the early return. Only the exact re-run (R4) closes this gap.
- The proxy over-tags the optimizer paths reached only by the 42 entries without `# execute: true`. It cannot be sized without R4.

### F3. Map state for tpcds
- 8899 lines carry tpcds. All 1004 lines where tpcds is the *only* test come from the whole-file fill:

  | File | Lines where tpcds is the only test |
  |---|---|
  | planner.py | 288 |
  | executor/python.py | 204 |
  | env.py | 191 |
  | table.py | 103 |
  | generators/python.py | 81 |
  | context.py | 71 |
  | executor/__init__.py | 66 |

- The fill includes blank, def and signature lines. `body_start()` therefore sees the def line as "recorded", which weakens the header test in the slow-test prune (selector.py:512).
- An edit to an `env.ENV` entry for a function that no TPC-DS query uses (e.g. `ARRAYANY`, `BITWISEXOR`) selects tpcds alone, at 23.5 s, although nothing can fail.

### F4. How the 5 sample commits pulled tpcds in (`work/why.log`, `out.jsonl`)

| Commit | Change | Route by which tpcds was selected | Can tpcds fail? |
|---|---|---|---|
| 2jrq | eliminate_joins.py:71, `and`→`or` | the line is covered by opt test_tpcds, so the proxy tags it | yes (fails) |
| 3utz | serde.py:53, `and`→`or` | native coverage plus the serde broadcast (`build_map.py:118-134`) | yes (fails, and tpch fails too) |
| 66ks | context.py:53, `is None`→`== None` | whole-file fill of `executor/` | no: `Table` defines no `__eq__` (table.py:10-75), so the mutant is **equivalent** |
| 2haz | dialect.py:1279, `is`→`==` on `NormalizationStrategy` | 1279 itself carries no tpcds and its slow test is pruned, but `changed_statement_lines` adds the statement head 1278, which carries tpcds (86 tests) | no: values come only from the enum (dialect.py:1169-1173), so the mutant is **equivalent** |
| 4d63 | snowflake Tokenizer `NUMERIC_ESCAPES` | **a false positive in fixture matching**, below | no: snowflake is never on the TPC path; tpcds has 0 lines in any snowflake file |

How 4d63 went wrong, step by step:
1. `import_time_change` calls `name_tests({"snowflake",...})`.
2. That reads `index["fixture_dialects"]["snowflake"]`, which includes `tests/fixtures/optimizer/optimizer.sql`.
3. `fixture_tests` matches `stem in s` as a **substring** (selector.py:341-344). The stem `"optimizer"` is found in the strings `'optimizer/tpc-ds/tpc-ds.sql'` and `'/optimizer/tpc-h/'`.
4. `build_map.test_strings` adds module-level constants to every test in the module. So all 60 TestExecutor tests match, tpcds, tpch and optimized_tpch among them, and so does test_scope (`'sqlglot.optimizer.scope'`).
5. `fixture_change` (selector.py:639) uses the same substring rule. A direct edit to optimizer.sql also selects all three TPC executor tests.

Removing tpcds from 66ks or 2haz by a "gate" would be unsound:
- context.py `Context.table` lies on every multi-table step of the executor.
- `case_sensitive` lies on the identifier-quoting path.
- A non-equivalent mutant on either line would fail tpcds.

Only equivalent-mutant detection can remove them (see R6).

### F5. Recall hole: `setUpClass` failures fail all 60 TestExecutor tests
- `TestExecutor.setUpClass` (test_executor.py:72-117) builds `Table(columns, rows)` for every TPC table. That runs table.py:17-23 plus `RowReader.__init__` and `RangeReader.__init__`. It also calls `load_sql_fixture_pairs` on both TPC files.
- pytest-cov records this under `<first test>|setup`. In the raw `.coverage`, only `test_group_by|setup` and `test_agg_order|setup` hold the 13 table.py lines, and `build_map.py` folds `|setup` into that test.
- In the map, table.py:17/19/23 carry 56 TestExecutor tests but **not** `test_execute_tpch`, `test_optimized_tpch`, `test_py_dialect` or `test_sql_boolean_logic_preserves_short_circuiting`.
- So a mutant such as `assert len(rows[0]) != len(self.columns)` at table.py:22 errors all 60 tests, and 4 of them are missed. That is +300 s.
- The same pattern exists for `TestOptimizer.setUpClass`: `MappingSchema()` covers 56 lines in schema.py, trie.py, dialect.py and helper.py, credited to 7 tests' `|setup` contexts. It also exists for `TestLineage.setUpClass`. There are 11 `|setup` contexts in the whole DB.

### F6. test_execute_tpch, the precise rule
- `tpc-h.sql` has no `# execute:` header, so `mp_execute` returns `None` (`:56-58`) and `subtestHelper` and duckdb are never reached.
- Its real dependencies are:
  - parsing the 22 TPC-H inputs (native, 1673 lines);
  - `serde.dump` (native) and `serde.load/_load` (child; the broadcast is right);
  - pickling the `Table` objects in `initargs` (plain pickle, no sqlglot code);
  - `setUpClass` (F5, currently missing);
  - the TPC-H inputs and meta lines, never the expected statements.
- The current map gives it 1769 lines, native plus serde. That is already tight. Only F5 needs to be added.

### F7. test_optimized_tpch
- It runs fully in process (`:157-163`) and needs no proxy. It reads **both** statements of each TPC-H pair: `(_, sql, optimized)`.
- It never calls the optimizer. It depends on the parser, the duckdb generator and both fixture halves, plus `setUpClass`.
- Its only waste is the 4d63-style fixture false positive.

### F8. Fixture pairs: who reads which half (helpers.py:44-55)
`load_sql_fixture_pairs` splits on `;` after dropping `--` and empty lines. Even statements are `meta + input`, odd statements are `expected`.

| Test | Reads input/meta | Reads expected |
|---|---|---|
| test_execute_tpcds, test_execute_tpch | yes | no |
| test_optimized_tpch | yes | yes |
| opt `check_file` (test_tpch, test_tpcds) | yes | yes |

- An edit confined to expected statements, with the same number of statements and loading cleanly, cannot fail either exec test or any other TestExecutor test.
- Today `fixture_change` on tpc-ds.sql selects all 60 TestExecutor tests (the module constant `DIR_TPCDS` matches the stem `tpc-ds`), so tpcds is included.
- On tpc-h.sql it selects the same 60, **including tpcds**, which never reads `tpch_sqls`.

### F9. Caution on the slow-test prune (selector.py:512-519)
- In dialect.py, line 1279 (the condition line of the multi-line `IfExp`) has 22 tests recorded, against 86 on lines 1278 and 1280.
- Line-level coverage of continuation lines is incomplete under 3.13. tpcds is absent at 1279 although it must evaluate it whenever it reaches 1278.
- The prune is safe today only because the statement head (1278) is added unpruned. Do not extend the prune to statement heads.

## (b) Savings on the sample and recall risk

| Recommendation | Sample effect | Hidden-set effect | Recall risk |
|---|---|---|---|
| R1 (fixture match by last path component) | 4d63 27.5 → 16.8 s, **−10.7 s** (LPT estimate with the scorer's cost rule, from `out.jsonl`); no other sample commit changes | any dialect-named-in-`optimizer.sql` change, or `optimizer.sql` edit, stops pulling the TPC tests (≈29 s) | low; misses only a test naming a fixture by a substring that is not its last path component (none at the pin, see R1) |
| R2 (`|setup` broadcast) | 0 s | +0–2 s on table.py, schema.py init or trie edits, where tpcds or a big set is usually already in | closes F5; adds no new miss shape |
| R3 (pair-half rule) | 0 s | ≈20 s saved per hidden fixture commit that edits only TPC expected text, and tpc-h input edits stop pulling tpcds. 8/30 sample commits are fixtures; P(≥1 such hidden commit) ≈ 30–50 %, so the expected value is ≈ −6 to −10 s | a fixture loaded by another loader (`load_sql_fixtures`, line-based) and wrongly treated as pair-only (guarded below) |
| R4 (exact executor coverage) | ≈0 s (2jrq, 3utz, 66ks and 2haz lines are all really on the path) | removes the 42-query over-tagging and the env/planner dead lines | closes the `leave_tables_isolated` under-tagging (F2) |
| R5 (env.ENV gate) | 0 s | small | moderate (fallback only, needs R4 absent) |
| R6 (equivalent-mutant rule) | **−37.2 s** (2haz 25.5 → 9.7, 66ks 24.8 → 3.3) | — | owned by whoever handles equivalent mutants; only the tpcds facts are given here |

Shapes of patch that would cause a miss:
- R1: a fixture referenced only by a constructed name. All `.sql` fixtures still have ≥1 match, and R1 keeps a substring tier before the directory fallback.
- R3: an expected-text edit containing `;` (pairing shifts). This is caught by comparing the input lists.
- R3: a new statement made only of `#` lines (an `IndexError` in `_extract_meta` → `setUpClass` crash). This is caught by simulating the load.

## (c) Code changes

**R1. `selector.py`: match fixtures by the last path component.** Add near `class_sql_names`:
```python
def fixture_ref(s: str) -> str | None:
    """The fixture a test string can name: its last path component, without .sql."""
    parts = [p for p in s.replace("\\", "/").split("/") if p]
    if not parts:
        return None
    last = parts[-1].strip()
    return last[:-4] if last.endswith(".sql") else last
```
In `Selector.__init__`, after `self.string_heads = ...`:
```python
        self.fixture_refs = {n: frozenset(filter(None, map(fixture_ref, v))) for n, v in self.strings.items()}
```
In `fixture_tests`, replace the memo line:
```python
            memo[stem] = ({n for n, refs in self.fixture_refs.items() if stem in refs}
                          or {n for n, strings in self.strings.items() if any(stem in s for s in strings)})
```
In `fixture_change`, replace the first `hits = ...` / `if hits:` block:
```python
        hits = {n for n, refs in self.fixture_refs.items() if stem in refs}
        if not hits:  # constructed names: fall back to substring before the directory rule
            hits = {n for n, strings in self.strings.items() if any(stem in s for s in strings)}
        if hits:
            return self.pair_half_filter(f, hits), "fixture-stem"   # R3; use `hits` if R3 is skipped
```
I checked the effect on every `.sql` fixture against the map (old substring rule → new rule). Every dropped match is a false positive:

| Fixture | Old → new | Strings that matched before |
|---|---|---|
| optimizer.sql | 231 → 169 | the 60 TestExecutor tests' path constants, `sqlglot.optimizer.scope` |
| canonicalize.sql | 167 → 1 | `canonicalize_table_aliases` (a meta key in `check_file`) |
| qualify_columns.sql | 167 → 1 | `validate_qualify_columns` |
| identity.sql | 5 → 2 | SQL text that contains "identity" |
| normalize.sql | 2 → 1 | `normalize_identifiers` |
| pretty.sql | 2 → 1 | `sqlglot.pretty` |

No fixture gains a match and no fixture drops to zero.

**R2. `tools/build_map.py`: broadcast `|setup` lines to the whole class** (`setUpClass`, `setUp`, class fixtures). In the context loop (around line 99):
```python
    setup_lines: dict[str, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    ...
            for ctx in contexts:
                if not ctx:
                    continue
                tid, _, phase = ctx.rpartition("|") if "|" in ctx else (ctx, "", "")
                if phase == "setup" and tid.count("::") == 2:
                    setup_lines[tid.rsplit("::", 1)[0]][rel].add(lineno)
                ...  # existing: tid -> index
```
After the `files` loop, before the pickle broadcast:
```python
    # setUpClass runs under the first test's "|setup" context on each xdist worker, but if it
    # raises, every test in the class errors. Give its lines to all tests of the class.
    for cls, per_file in setup_lines.items():
        members = {i for i, tid in enumerate(ids) if tid.startswith(cls + "::")}
        for rel, lns in per_file.items():
            lines = files[rel]["lines"]
            for ln in lns:
                lines[str(ln)] = intern(set(set_table[lines[str(ln)]]) | members)
```
`data.update(.coverage.opt)` keeps contexts, so the thread re-run's setup lines are included too.

**R3. `selector.py`: pair-half filter for fixtures read through `load_sql_fixture_pairs`.**
- `tools/build_index.py` adds `"pair_no_expected"`: test ids of a class whose test body and same-class helpers it calls (`self.name(...)`, transitively) contain **no** 3-element tuple target whose third element is anything but `_`. Restrict it to classes whose module calls `load_sql_fixture_pairs` and never `load_sql_fixtures`.
- At the pin this set is every TestExecutor test except test_optimized_tpch, plus any other test that unpacks a 3-tuple. Over-including in "reads expected" is harmless.

Selector side:
```python
def fixture_pairs(text: str):
    stmts = "\n".join(l for l in text.splitlines() if l and not l.startswith("--")).split(";")
    out = []
    for i in range(0, len(stmts) - 1, 2):
        lines, j, meta = stmts[i].strip().split("\n"), 0, {}
        while lines[j].startswith("#"):          # same IndexError as helpers._extract_meta
            k, v = lines[j].split(":", 1)
            meta[k.lstrip("#").strip()] = v.strip()
            j += 1
        out.append((meta, "\n".join(lines[j:]), stmts[i + 1].strip()))
    return out

    def pair_half_filter(self, f: dict, hits: set[int]) -> set[int]:
        """Drop tests that never read the expected half when only expected halves changed."""
        if Path(f["path"]).suffix != ".sql" or not (f["old"] and f["new"]):
            return hits
        if any(c in t for h in f["hunks"] for tag, t, _ in h["lines"] if tag in "+-" for c in SPLITLINES_EXTRA):
            return hits
        old = self.source(f["old"])
        new = apply_patch(old, f) if old is not None else None
        try:
            a, b = fixture_pairs(old), fixture_pairs(new)
        except Exception:
            return hits                          # the load itself may crash: keep everything
        if len(a) != len(b) or any(x[:2] != y[:2] for x, y in zip(a, b)):
            return hits                          # an input or meta line changed
        skip = {self.by_id[t] for t in self.index.get("pair_no_expected", []) if t in self.by_id}
        return (hits - skip) or hits
```
Extension (optional, also sound): when only the *inputs* of `tpc-h.sql` change and the new file loads, drop `test_execute_tpcds`. It references only `tpcds_sqls`, through `self.tpcds_sqls` in `test_execute_tpcds`. Derive "fixture-attribute readers" in build_index by mapping `cls.X = list(load_sql_fixture_pairs("F"))` in `setUpClass` to the tests that touch `self.X`.

**R4. Exact executor coverage (map build, never charged).**
- `work/pytest_exec.log` shows the `tools/coverage_executor.sh` run started (`bringing up nodes`, subtest marks) and was killed before writing `.coverage.exec`.
- Rerun it without xdist (drop `-n 2`; with xdist, coverage is combined only at the very end) and with a generous timeout, as one user-side step.
- Then make the proxy a fallback, as round-1 07(c) describes:

```diff
-    tpc_exec = {index[t] for t in ("tests/test_executor.py::TestExecutor::test_execute_tpcds",) if t in index}
+    tpc_exec = set() if (ROOT / ".coverage.exec").exists() else \
+        {index[t] for t in ("tests/test_executor.py::TestExecutor::test_execute_tpcds",) if t in index}
```
- Keep the serde broadcast either way: the thread re-run skips pickling.
- Keep R2: thread-mode `setUpClass` contexts behave the same.

**R5. Fallback only (no `.coverage.exec`): fill env.py by ENV key, not the whole file.**
- At map build, parse the input and expected SQL of the 57 `# execute: true` TPC-DS entries (build_map may import sqlglot in the container) and collect `{type(n).key.upper()}`.
- Fill tpcds on module-level lines, helper functions, and only those `ENV` dict items (`ast.Dict` keys at env.py:249) whose key is in the set.
- Add a fixed safety set covering operators the planner introduces: `AND`, `OR`, `NOT`, `EQ`, `NEQ`, `GT`, `GTE`, `LT`, `LTE`, `ADD`, `SUB`, `MUL`, `DIV`, `CAST`, `ORDERED`, `COUNT`, `SUM`, `AVG`, `MIN`, `MAX`, `IN`, `BETWEEN`, `COALESCE`, `CASE`, `IF`, `NULLIF`, `INTERVAL`.
- The risk is a class introduced only by `leave_tables_isolated=True` rewrites. It is low value; prefer R4.

## (d) Ranked recommendations

1. **R1, fixture last-path-component match.** −10.7 s on the sample, plus hidden dialect/optimizer.sql cases. Risk: **low** (verified on all `.sql` fixtures; substring tier kept).
2. **R2, `|setup` broadcast in build_map.** ≈0 s, and closes a 300 s miss shape (Table/RowReader init mutants; also TestOptimizer's `MappingSchema()` setup). Risk: **none** for recall; +0–2 s cost on those lines.
3. **R6 (cross-ref), equivalent-mutant detection.** −37 s on the sample; this is where most of the tpcds waste is. Two sound cases at the pin:
   - `X is None` ↔ `X == None` when no candidate class defines `__eq__`. `Table`/`Context` define none; `Expr.__eq__` (core.py:837) returns False for `None`.
   - `is` ↔ `==` against members of a `(str, Enum)` whose attribute is assigned only enum values. Example: `normalization_strategy`, dialect.py:1169-1173.

   Risk: **medium**; it must be proven per pattern.
4. **R3, pair-half rule for TPC fixtures.** Expected value ≈ −6 to −10 s on hidden. Risk: **low** with the load simulation and the input-equality guard.
5. **R4, rerun `coverage_executor.sh` serially; proxy becomes fallback.** Sample ≈0 s; hidden precision gain, and recall for `leave_tables_isolated` shapes. Risk: **low** (verify `merged .coverage.exec` and a non-trivial tpcds count in simplify.py).
6. **R5, ENV-key gate for env.py.** Small gain. Risk: **medium**. Only without R4.

Do not try to gate tpcds on optimizer, planner or executor lines by "shared with cheap tests". In this suite, dominance does not help, because every failing test must be selected. For an on-path, non-equivalent change, P(tpcds fails) is well above the 3–7 % break-even; 2jrq is the example.
