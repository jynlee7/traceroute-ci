# R2-10: edits to lines no test covers (dead-code rule)

Method: read-only inspection of `maps/impact.json.gz`, `work/sqlglot/**`, `selector.py`, `tools/build_map.py`, and `sample/commits/*.patch`. Nothing was run. Counts come from small `ast` + json one-liners.

## 1. What selector.py does today for a line with no coverage

A line that no test touched is **absent from the map's `lines` table**. `tools/build_map.py` records every executed line (`lines[str(lineno)] = intern(tests)`), including lines run outside any test context. Those get the empty set 0, the "I" class below. So an absent line was never executed anywhere in the whole pytest run.

`sqlglot_change` (selector.py ~L483-583) handles a changed line inside a function (`fn`, not import-fn) in this order:
1. Window: `cov(fn[0..], ln±WINDOW)`, with WINDOW=2, clipped to the function (L529). It usually finds neighbouring executed lines, so the result is "the tests that ran next to it".
2. If the window is empty: the **whole function** (`hit = self.cov(rel, range(fn[0], fn[1]+1))`).
3. If that is empty and `cls` exists: the **whole innermost class** (`if not hit and cls`).
4. If that is empty but the function has any table entry (its `def` line runs at import): `import_time_change(...) or set(self.all)` ("fn-import-only"), i.e. the whole dialect family or everything.
5. A changed line outside any function goes to the `else` branch: lambda, then `package-init` returns ALL for any `__init__.py`, then `import_time_change`, with `module-level-unmapped` returning ALL.

So the answer to "function, class, or file?" is: **window, then function, then class, then file family, then ALL**. Nothing ever returns `[]` for a dead line. The class step is the expensive one.

Simulated step distribution for every dead leaf statement inside a function (1165 statements, same fallbacks computed against the map):

| step reached | stmts | median tests selected |
|---|---|---|
| window | 773 | 4 (mean 35, 13 stmts >= 300) |
| whole function | 189 | 14 |
| **whole class** | 175 | **1540 (157 of them >= 300)** |
| fn-import-only (family or ALL) | 28 | n/a, the family or ALL fallback |

The class case is typical of dead methods on big classes (`Expr` stubs, `*Generator` classes), where one edit pulls in nearly the whole suite.

## 2. Size of the uncovered set (pinned commit)

Counts are leaf statements (docstrings excluded). I used the statement extent, not the first line, because coverage records a multi-line `if (...)` on a later line. Counting only the first line wrongly inflated the dead count from 1333 to 1685.

| class | meaning | stmts | share |
|---|---|---|---|
| T | executed in a test context | 28450 | 78.4% |
| I | executed only with no test context: import, class creation, setUpClass, collection, unmatched ids, and the executor/planner/python-generator padding added by build_map.py | 6176 | 17.0% |
| A | never executed at all ("dead") | 1333 (leaf) | 3.7% |

Statements of kind A with the whole extent absent: 1333.
- In a function that otherwise ran: 847.
- In a function whose body never ran: 318. There are 206 dead functions out of 3565; 109 of them are the abstract `raise NotImplementedError` stubs of `Expr` in `expressions/core.py:131-330`.
- At module or class level: 168. Of these, 143 are under `t.TYPE_CHECKING`, 22 are `__main__.py` / `args.parse`, and 3 are the `not SQLGLOTC_INSTALLED` fallback.

The biggest absolute dead counts are `generators/duckdb.py` (about 200 by first-line count), `parser.py`, `generator.py`, `expressions/core.py`, `optimizer/qualify_columns.py` and `simplify.py`.

## 3. Genuinely dead versus blind spots

**Not blind spots, because they never become "absent":**
- setUpClass, module-level test code and collection run with an empty context. build_map.py skips the empty context but still interns the line as set 0, so these are class I and the existing import-time logic keeps them.
- Contexts whose test id is not in `tests.json` are also class I.
- Lines with a skipped or failing test: the coverage run had 1620 passed, 1 skipped (`pytest.log`), and the final run uses the same env.

**Real blind spots, where a child process runs code the parent coverage never sees:**
- `tests/test_executor.py:176` (`ProcessPoolExecutor`, spawn) runs `execute()` in children. `build_map.py` pads `executor/*`, `planner.py` and `generators/python.py` with empty sets (they become I), and gives lines that run in the TPC proxies `tpc_exec`.
- But the children also run `optimize(..., leave_tables_isolated=True)` (`executor/__init__.py:76`), the Python generator through the base `generator.py`, `schema.py`, `expressions/*` and `helper.py`. Lines there that **no** in-process test runs would show as A while really being live in `test_execute_tpcds`. `leave_tables_isolated=True` is used only by `execute`, so `merge_subqueries` branches are a concrete example. `.coverage.exec` does not exist and `pytest_exec.log` ends at "bringing up nodes...", so there is no real coverage of these children.
- `tests/test_optimizer.py:111` uses a spawn pool. The map handles it by merging a thread-pool re-run (`.coverage.opt`, 167 tests). That is exact for optimizer code.
- `tests/dialects/test_dialect.py:234` runs a subprocess that only imports sqlglot. Import-time lines are already class I.

**Static-semantics hazards for an edit to a dead line:**
- A new local binding in a dead branch shadows a global the live code reads (UnboundLocalError).
- A new `yield` or `await` changes what the function is.
- `global` / `nonlocal` / `del` changes.
- Inserted text with different indentation swallows live statements into another block.
- A dead line inside the `else:` or `except` structure (not itself a statement) changes control flow.
- Module or class-level dead statements (conditional `def`s, names that importers read).

**Conclusion:** a dead statement is safe to treat as unreached when all of these hold.
- It is in dialect-specific code the TPC children never reach. This allowlist covers `sqlglot/dialects/*` (not `dialect.py`, `__init__`), `generators/*` (not `python.py`, `__init__`), `parsers/*`, `lineage.py`, `diff.py`, `anonymize.py` and `__main__.py`. It holds **497 of the 1333 dead statements (37%)**: generators 322, parsers 122, `__main__` 22, lineage 21, diff 6, anonymize 2, dialects 2. It excludes the 836 in the base generator, parser, optimizer, expressions, schema, executor and helper.
- The edit passes the structural checks in section 5.

Files outside the allowlist keep today's behaviour, so there is no regression there.

## 4. Sample patches

I classified the old line of every removed line in the 30 patches (all are one-line mutations).

| class of touched line | commits |
|---|---|
| T | 2haz 2jrq 2qn5 3utz 4d63 535z 5y72 66ks 6ze8 ary6 bj7g fryt gbav j6uz rszg(typing) |
| I (import-time, keep) | ktz3 (`expressions/ddl.py:82`) |
| ast-identical (already `[]`) | 986c 9u5k bap8 bvw2 f4xa rszg(scope) |
| **A, dead** | **8y7j** (`generators/clickhouse.py:55`) |

8y7j: `if scale == exp.UnixToTime.MICROS:` becomes `!=`. Lines 55-58 are never executed. Lines 53-54 (MILLIS) are executed by 1 test, so the ±2 window returns that 1 test (why.log: `8y7j ... fn:1`, about 2.5 s). The dead rule returns `[]`.

**Savings on the sample: about 2.5 s from 8y7j.** That is 1 of 30 commits, in line with the 3.7% dead-statement rate. For the ~45-commit hidden set, expect about 1-2 such commits at about 2.5 s each if they land in the window case. If one lands in the class case it saves roughly 60-100 s, and about 13% of dead edits are that case. The 2qn5 and 5y72 "should be []" commits edit **covered** lines, so no coverage rule can help them; that is mutation-equivalence, not dead code.

## 5. Proposed rule: `dead-code`

Preconditions, all required. Otherwise fall through to the current logic unchanged.
1. The file is in the allowlist (section 3).
2. Every touched old line is in a dead statement's extent, or is blank or comment-only. A touched `else:`, `except`, `finally` or `def` line is not dead.
3. **Skeleton equality.** Remove from the old tree every statement whose whole extent is dead. Remove from the new tree every statement whose lines are all `+` lines or context lines mapping to dead old lines. The two `ast.dump`s must be equal. This guarantees the live structure and indentation did not change.
4. **Scope signature.** For each function containing a dead touched line, compare old and new by qualname: yield, await, global and nonlocal counts equal, and `(bound_old ^ bound_new) ∩ names loaded in the live skeleton` empty. A missing function means fall back.

The position argument: an added statement sits exactly where the old dead statement sat in an unchanged live skeleton. It is reached only when control reaches a spot no test reaches.

Where it goes: a new method on `Selector`, called at the top of `sqlglot_change` right after `changed`, `removed` and `prunable` are computed. When it holds, it removes the dead lines from `changed`. Remaining live lines still go through the normal loop, so mixed patches only keep their live lines. The new-def, lost-name and `typing/` logic after the loop is untouched, and its reason is added to `reasons`.

```python
# --- selector.py, near the top (module constants) ---
DEAD_OK_DIRS = ("sqlglot/dialects/", "sqlglot/generators/", "sqlglot/parsers/")
DEAD_OK_FILES = {"sqlglot/lineage.py", "sqlglot/diff.py", "sqlglot/anonymize.py", "sqlglot/__main__.py"}
DEAD_NEVER = {"dialect.py", "__init__.py", "python.py", "base.py"}  # base names: tpcds children reach them


def dead_allowed(rel: str) -> bool:
    return (rel.startswith(DEAD_OK_DIRS) and rel.rsplit("/", 1)[-1] not in DEAD_NEVER) or rel in DEAD_OK_FILES
```

```python
# --- selector.py, module level helpers ---
def _is_doc(n) -> bool:
    return isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)


def dead_stmt_lines(tree, table: dict) -> set[int]:
    """Lines inside a statement none of whose lines coverage ever recorded."""
    out: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.stmt) and not _is_doc(n):
            rng = range(n.lineno, n.end_lineno + 1)
            if all(str(x) not in table for x in rng):
                out.update(rng)
    return out


def drop_dead(tree, dead: set[int]):
    """Copy of the tree without statements whose whole extent is in `dead`."""
    tree = copy.deepcopy(tree)
    for n in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            seq = getattr(n, field, None)
            if isinstance(seq, list):
                seq[:] = [s for s in seq if not (isinstance(s, ast.stmt) and not _is_doc(s)
                          and all(x in dead for x in range(s.lineno, s.end_lineno + 1)))]
    return tree


def new_dead_lines(f: dict, old_dead: set[int]) -> set[int]:
    """Dead lines in the new file: added lines, plus context lines that were dead."""
    out: set[int] = set()
    delta = 0
    for h in f["hunks"]:
        for tag, _text, old_no in h["lines"]:
            if tag == "+":
                out.add(old_no + delta); delta += 1
            elif tag == "-":
                delta -= 1
            elif old_no in old_dead:
                out.add(old_no + delta)
    return out


def _scope_sig(fn, live_loaded: set[str]):
    bound, flags = set(), []
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            bound.update((a.asname or a.name).split(".")[0] for a in n.names)
        elif isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n is not fn:
            bound.add(n.name)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        elif isinstance(n, (ast.Yield, ast.YieldFrom, ast.Await, ast.Global, ast.Nonlocal)):
            flags.append(type(n).__name__)
    return bound & live_loaded, tuple(sorted(flags))


def _qualfns(tree) -> dict:
    out, stack = {}, []
    def visit(node, prefix):
        for c in ast.iter_child_nodes(node):
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = f"{prefix}.{c.name}"
                k = q
                while k in out:
                    k += "'"          # same-name redefinition: order index
                out[k] = c
                visit(c, q)
            else:
                visit(c, prefix)
    visit(tree, "")
    return out
```

```python
# --- method on Selector ---
    def dead_lines(self, f, old_tree, new_tree) -> set[int]:
        """Touched old lines that no test (or import, or child process we know of) can reach.
        Empty set unless every precondition holds."""
        rel = f["path"]
        table = self.files.get(rel, {}).get("lines", {})
        if not dead_allowed(rel) or not table:
            return set()
        old_src = self.source(rel).splitlines()
        old_dead = dead_stmt_lines(old_tree, table)
        touched = touched_old_lines(f)
        neutral = {n for n in touched if 1 <= n <= len(old_src)
                   and (not old_src[n - 1].strip() or old_src[n - 1].lstrip().startswith("#"))}
        dead = {n for n in touched if n in old_dead}
        if not touched or (touched - dead - neutral):
            return set()                     # a live, header or structural line is touched
        new_dead = new_dead_lines(f, old_dead)
        old_skel, new_skel = drop_dead(old_tree, old_dead), drop_dead(new_tree, new_dead)
        if ast.dump(old_skel) != ast.dump(new_skel):
            return set()
        old_fns, new_fns = _qualfns(old_tree), _qualfns(new_tree)
        skel_fns = _qualfns(old_skel)
        for q, fn in old_fns.items():
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(fn.lineno <= n <= fn.end_lineno for n in dead):
                continue
            nf = new_fns.get(q)
            if nf is None or q not in skel_fns:
                return set()
            loaded = {x.id for x in ast.walk(skel_fns[q]) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load)}
            if _scope_sig(fn, loaded) != _scope_sig(nf, loaded):
                return set()
        # dead lines outside any function (module/class level) are not accepted
        if any(not any(fn.lineno <= n <= fn.end_lineno for fn in old_fns.values()
                       if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))) for n in dead):
            return set()
        return dead | neutral
```

Hook in `sqlglot_change`, right after `renamed` is computed and before `out = set()`:

```python
        dead = self.dead_lines(f, old_tree, new_tree)
        if dead:
            changed = {c for c in changed if c not in dead}   # changed was widened to statement start lines
            dead_reason = "dead"
```
and at the end `reasons` gets `"dead"` when any lines were dropped. If `changed` is then empty, the loop does nothing and the result is `[]` plus the existing `new-def` / `lost` logic, which only fires if the patch also adds or removes definitions. Also add `import copy` at the top of selector.py.

Notes on the code:
- `changed_statement_lines` widens touched lines to the first line of each simple statement. For a dead multi-line statement that first line is in `old_dead` too, so the filter is consistent.
- `touched_old_lines` already puts both neighbours of a pure insertion into `touched`, so insertion-at-the-edge of a dead block falls back by itself.
- The code assumes hunk lines are `(tag, text, old_no)` with `old_no` the following old line for `+`. This is consistent with `touched_old_lines` (L141-160) and `same_indent_replacements`. It was not executed, so a reviewer should unit-check `new_dead_lines` first.

Optional second tier (small, low value): module-level dead lines under `if t.TYPE_CHECKING:` (143 stmts, mostly `sqlglot/__init__.py:58-60`). Today these hit `package-init` and run ALL. Skip them unless the file has `from __future__ import annotations`, because the edit has no runtime effect. Only for files whose module scope contains only that guard.

## 6. Recall risk and miss shapes

Why the rule is sound on the pinned map: any dead line was executed by no test, no import, no setUpClass and no collection, so the old behaviour of every test is independent of its text. The checks in section 5 cover the ways a text edit can still change *other* lines' behaviour.

Shapes that would cause a miss (all excluded or conservatively rejected):
- New local that shadows a global read by live code: caught by the scope signature.
- New `yield` / `await`, or `global` / `nonlocal` changes: caught.
- Edit to an `else:` / `except` / `finally` / `def` / decorator line: not dead, so it falls back.
- Indentation change that re-nests live code: caught by skeleton equality.
- Dead line reachable only from a tpcds child process: excluded by the allowlist. The remaining theoretical case is the base `Dialect` plumbing, which is why `dialect.py`, `base.py`, `__init__.py` and `python.py` are excluded.
- Dead code in dialect files that import-time plumbing reads through introspection (`Dialect.classes`, `__init_subclass__`): not statements in a function body, so the function-only requirement rejects them.
- A test that compares source text or line numbers: `grep` of `tests/` found only `test_typing.py:41` (reads `typing/*.py`, outside the allowlist) and `test_docs.py` (doctest collection, which is not run under pytest here; docstring edits are already handled by `docstring-only`).
- Stale map: if hidden commits are against the pinned source and the map is the pinned map, absence is exact. If a patch is stacked on one that changes line numbers, the line numbers still refer to `work/sqlglot` (selector already assumes this).

Residual risk: low (rated 2/10). Worst realistic failure: a dead line that is live only under a test configuration absent from the coverage run, such as a skipped `skipIf(_EXPRESSION_IS_COMPILED)` test. These are mypyc-only paths; the coverage env was pure Python and so is the scored run.

## 7. Ranked recommendations

1. **Dead-code rule, allowlisted, with skeleton and scope checks** (section 5). Saves about 2.5 s on the sample (8y7j). Expected hidden gain is 3-10 s, with an upside of about 100 s per class-expansion case. Risk: low (2/10). Do not drop the skeleton check, because it is what makes pure insertions safe.
2. **Cap the class step** (selector.py ~L531, `if not hit and cls:`). For a dead line, taking the whole class is the largest single over-selection (median 1540 tests). For lines in files outside the allowlist, replace the class fallback with the whole function plus tests that call the method name (`name_tests`), and fall to the class only if the function has no coverage and the name has no callers. Saves about 0-100 s on rare commits. Risk: medium (5/10), because it is not backed by a dead proof. Prefer to limit it to the allowlisted files, where item 1 already returns `[]`.
3. **Pad children in `build_map.py`**: add a `.coverage.exec` rerun (thread-pool executor test, like `coverage_optimizer.sh`) so tpcds children contribute real line coverage. That would let the allowlist grow to `optimizer/*`, `generator.py` and `schema.py` (another 836 dead statements), and would also tell us which of those lines tpcds really runs. Expected saving: about 2-3 s per hidden dead optimizer edit, and it can cut `test_execute_tpcds` (21.5 s) from edits that today reach it only through the proxy padding. Risk: low (2/10) for the map change itself; it needs a Docker run, which is the coordinator's call, not part of this research.
4. **Module-level `TYPE_CHECKING` edits** return `[]` instead of ALL via `package-init`. Saves nothing on the sample, and about 0-1 hidden commits at up to ~100 s each. Risk: low (3/10) if limited to `if t.TYPE_CHECKING:` bodies in a file with `from __future__ import annotations`.
5. Do **not** extend the rule to class I lines (17% of statements). They are import-time or setUpClass-time, and the existing import-time logic (`import_time_change`, `in_import_fn`) is correct to keep them safe.
