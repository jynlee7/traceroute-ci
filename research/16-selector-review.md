# 16 — selector.py correctness review

Reviewed `selector.py` as it stood on 2026-11-21 (834 lines: the version with the `parse_patch` rewrite,
`bytes_inert`, `strip_docstrings`, `import_fns`, `test_module_reach`, `module_bound`/lost-import,
slow-test pruning via `body_start`, and the LPT+improve `split`). I also read `tools/build_index.py`
(the new keys `import_fns`, `test_rdeps`, `sqlglot_names`, `all_dialect_tests`, `fixture_dialects`).
I ran nothing. As a static check only, I ran `ast.parse` on both files and `ruff check --select F`
(undefined or unused names). Both passed.

## (a) Findings

### Syntax, NameErrors, Python 3.13: no defects
- Both files parse, and ruff's F rules report nothing: no undefined names, typos or unused imports.
- Python 3.13: the code uses no removed APIs (`ast.Str`, etc.). `ast.dump` in 3.13 hides empty
  fields by default, but `python_change` (:412, :419) dumps both trees the same way, so the
  comparison is unaffected. `strip_docstrings` inserts a bare `ast.Pass()` (:677). It has no
  `lineno`, but its `end_lineno` defaults to `None`, so `changed_statement_lines` (:704,
  `not node.end_lineno`) short-circuits before it reads `lineno`. Safe.
- In 3.12+, NUL bytes raise `SyntaxError` rather than `ValueError`. The handlers at :401 and :409
  catch that.
- `-S` is fine: only the standard library is imported (`bisect` is new and is stdlib).

### Output validity per SCORING.md: OK
- Every number comes from `tests.json` (`self.all`, `by_id`, `number_of` with `None` dropped,
  `all_dialect_tests` through `by_id.get`), and sets make tests unique. `split` (:786) uses
  `kmax = min(8, len)`, which means no empty bins, and it filters empties anyway. It emits sorted
  int lists.
- One line per patch, printed after the `try`. Init failure falls back to every test (:811–815).
  Logs go to stderr. `json.dumps` escapes to ASCII, which is valid.
- `_lb_ms` (:725) is a valid lower bound (pigeonhole `d[k-1]+d[k]`), so the early `break` (:797)
  is sound. `_improve` (:743) only applies moves or swaps with `0 < d < gap`, and keeps `loads`
  and `bins` consistent.
- `split()` sits outside the per-commit `try` (:828). It can only raise if `chosen` holds a
  number that is not in `ms`, and none of the code paths can produce one.

### Exceptions that would push a commit onto the run-everything fallback: none likely
I checked each indexing and unpacking site: `lam[0]` is guarded by `hit` (:480), `cur["hunks"][-1]`
only runs when `in_hunk`, `unquote_path` has a `try`, and the index entries unpack with the shapes
`build_index` writes. What remains is `RecursionError`/`MemoryError` from `ast.parse` on
pathological input, which correctly gives "all tests".

### Earlier risks now fixed (confirmed)
- Renames go to `select` (:588) before the AST comparison, so a rename plus whitespace no longer
  gives `[]`.
- `test_dialect.Validator` edits now reach the 33 importing modules (`test_module_reach`, :559).
- Lambda shortcut: inserting next to a lambda, or editing its key line, now also takes the
  import-time rule (:480–498).
- Import-time hooks (`Expr.__init_subclass__`: the old rule selected 2 tests; `_Tokenizer.__init_subclass__`:
  65) now take `import_time_change` (:447).
- Hunk counts bound the hunk lines (format-patch trailers, blank context), `-N,0` insertions are
  placed correctly (:81, :168), and `"\r"` is stripped.

## (b) Recall risks (patch shapes that would miss) and cost waste

**R1. HIGH: slow-test pruning drops slow tests on lines that coverage never records (:458–462).**
`exact = self.cov(rel, [ln])` is empty whenever `ln` is not a recorded line:
- continuation lines of a compound header, e.g. editing line 2 of `if (a and\n b):` or of a
  multi-line `for`/`while`/`with`/`elif` condition. Coverage reports these on the header's first
  line, and `changed_statement_lines` only widens *simple* statements;
- pure-insertion anchors that are both a comment or a blank line;
- dedenting moves: `-        x = f()` in a branch the slow test skips, `+    x = f()` now
  unconditional. The removed line was never run by the slow test, so the test is pruned even
  though it now runs the moved code.

These cases drop `test_max_nodes`/`test_execute_tpcds`/`test_generator_memory`, each a 300 s miss.

**R2. HIGH (if mutation-style patches occur): removing or renaming a module-level name that
another sqlglot module imports.** `lost` (:499) only looks up `sqlglot_names`, which lists names
that *test* modules import. If, say, `seq_get` is removed from `helper.py` while `parser.py` still
imports it, importing sqlglot fails and every test fails. The selector takes only the coverage of
the removed function's body. The same applies to removing a top-level class that another module
names at import time (`exp.Foo: lambda …` in a class table raises `AttributeError` at import).

**R3. MED: `sqlglotc/` renames select nothing (:582, :590).** `code_paths` omits `sqlglotc/`, so
renaming or deleting `sqlglotc/setup.py` through a rename header gives `rename-other` → `[]`. But
`tests/test_sqlglotc.py` runs `sqlglotc/setup.py` through `runpy.run_path`.

**R4. MED (only if the index build failed): `test_file_change` falls back to the edited file
only (:559).** Without `test_rdeps`, a `Validator` edit selects `test_dialect.py` and misses the 33
importing modules. When the index exists, `reach` is never `None` for a file that has tests, so
this is purely the index-missing path.

**R5. LOW: lambda key on a shared line (:478–480).** `lambda_range` picks the *smallest* lambda
containing `ln`. On a line like `), "BAR": lambda …`, the earlier lambda that ends on the line can
win. The edited key `"BAR"` then takes the lambda-only path and the new key's users are missed.

**R6. LOW: `module_bound` (:610) ignores names bound under top-level `if`/`try`/`with`**
(`try: from x import y`, `if t.TYPE_CHECKING:`). If such a name is removed, the lost-import rule
never sees it.

**Cost and CPU waste (no recall impact):**
- C1. `import_time_change` is recomputed for every changed line (:450, :473, :494). A 30-line
  class-table edit in a dialect therefore repeats family `file_cov` + `name_tests` 30 times.
  `fixture_tests` (:336) rescans about 34k strings for each fixture path, and the `dialect.py`
  family has ~35 words × up to 15 fixtures. Together these can cost hundreds of ms of charged CPU
  on one commit.
- C2. `.py` files outside `sqlglot/`/`tests/` reach `python_change` → `source()` is None → all
  (:593 runs before :597/:601). This covers `benchmarks/*.py`, `docs/**/*.py`, `.github/**/*.py`
  and `sqlglotc/*.py`, which the dedicated `sqlglotc` rule therefore never sees.
- C3. Binary files under `tests/fixtures/` (`tpc-h/*.csv.gz`) select all tests (:586).
  `fixture_change` only needs the path.
- C4. A new test module that is not in `by_file` and has no importers gives `reach is None` →
  all (:535).
- C5. `build_index` `scan()` seeds `import_fns` with *attribute* callee names from module and
  class-body calls (`", ".join(...)` → `join`). Any same-file def with that name, such as
  `Select.join`, becomes an import-time function and selects the whole file. A metaclass in a
  file also marks *every* `__init__`/`__call__` in that file (for example `Dialect.__init__` in
  `dialect.py`).
- C6. `def_names` (:630) counts defs nested in functions, so a new local helper named `_builder`
  or `transform` pulls every same-named def's callers through `new_method_tests`.
- Note: `strip_docstrings` mutates `old_tree`/`new_tree` in place (:417), and the trees then go
  into `sqlglot_change`. This is harmless today (docstring lines are not executable), but worth a
  comment.

## (c) Exact fixes

**F1 (R1), in `Selector`: nearest recorded line, plus no pruning on dedent.** Add these methods:
```python
    def exact_cov(self, rel: str, fn: tuple[int, int], ln: int) -> set[int]:
        """Coverage of ln, or of the nearest recorded lines above and below it inside fn
        (continuation lines, comments and blank anchors are never recorded)."""
        table = self.files.get(rel, {}).get("lines", {})
        if str(ln) in table:
            return self.cov(rel, [ln])
        out: set[int] = set()
        for step in (-1, 1):
            x = ln + step
            while fn[0] <= x <= fn[1]:
                if str(x) in table:
                    out |= self.cov(rel, [x])
                    break
                x += step
        return out
```
Add this module-level function:
```python
def dedents(f: dict, old_text: str) -> bool:
    """Some added code is shallower than the code it replaces or follows (moved out of a branch)."""
    old_lines = old_text.split("\n")
    ind = lambda s: len(s) - len(s.lstrip())
    for h in f["hunks"]:
        rem, add, at = [], [], None
        for tag, text, old_no in h["lines"] + [(" ", "", 0)]:
            if tag != " ":
                at = old_no if at is None else at
                (rem if tag == "-" else add).append(text)
                continue
            added = [ind(t) for t in add if t.strip()]
            base = [ind(t) for t in rem if t.strip()]
            if added and not base and at:
                prev = next((old_lines[i] for i in range(min(at, len(old_lines) + 1) - 2, -1, -1)
                             if old_lines[i].strip() and not old_lines[i].lstrip().startswith("#")), None)
                base = [ind(prev)] if prev is not None else []
            if added and base and min(added) < min(base):
                return True
            rem, add, at = [], [], None
    return False
```
In `sqlglot_change`, after :442 add
`no_prune = dedents(f, self.source(f["old"]) or "")`, then replace :458 and :461 with:
```python
                if hit & self.slow and not no_prune:
                    ...
                    exact = self.cov(rel, range(fn[0], fn[1] + 1)) if ln <= self.body_start(rel, fn) else self.exact_cov(rel, fn, ln)
```

**F2 (R2), in `build_index`: an index of names imported between sqlglot modules.** Inside the
existing import loop (the `for node in ast.walk(tree)` that fills `rdeps`), for
`ast.ImportFrom`, add:
```python
                    if f and f != rel and isinstance(node, ast.ImportFrom):
                        for a in node.names:
                            internal_imports.setdefault(f"{f}::{a.name}", set()).add(rel)
```
Declare `internal_imports: dict[str, set] = {}` and return
`"internal_imports": {k: sorted(v) for k, v in internal_imports.items()}`. Note: `f` there is the
module file of `t`. Restrict this to `t == base`, so only the module imported *from* counts.
In `selector.py`, `select` must pass the patched paths: `self._patched = {x["path"] for x in files}`
at the top of `select`. Then at :500, before the test-module lookup:
```python
            imp = self.index.get("internal_imports", {})
            refs = self.index.get("refs", {})
            for n in lost:
                if any(u not in self._patched for u in imp.get(f"{rel}::{n}", [])):
                    return set(self.all), "lost-internal-import"
                if any(r != rel and s == e and r not in self._patched for r, s, e in refs.get(n, [])):
                    return set(self.all), "lost-referenced-class"  # e.g. exp.Foo in another module's table
```
`refs` entries with `start == end` are name tokens. Entries with `start != end` are `<lower>_sql`
bodies and are skipped.

**F3 (R3), :582:** `code_paths = ("sqlglot/", "tests/", "sqlglotc/")`. Also move the `sqlglotc/`
branch (:597–600) above `elif suffix == ".py":` (:593). That fixes C2 for sqlglotc.

**F4 (R4), :559:** `return reach if reach is not None else set(self.all)`.

**F5 (R5), :480:** add `and not re.search(r"""["'\w]\s*:\s*lambda\b""", self.source(f["old"]).split("\n")[ln - 1])`
to the shortcut condition. This handles any line that starts a table entry like a key line.

**F6 (C1): memoise.** In `__init__`, add `self._itc = {}` and `self._fx = {}`. Then:
```python
    def import_time_change(self, rel, line):
        key = (rel, self.top_class(rel, line))
        if key not in self._itc:
            self._itc[key] = self._import_time_change(rel, line)  # rename the current body
        return set(self._itc[key])
    def fixture_tests(self, fixture_rel):
        if fixture_rel not in self._fx:
            stem = Path(fixture_rel).stem
            self._fx[fixture_rel] = {n for n, ss in self.strings.items() if any(stem in s for s in ss)}
        return self._fx[fixture_rel]
```
In `name_tests`, dedupe first: `fxs = {fx for w in words for fx in fixture_dialects.get(w, [])}`.

**F7 (C2, C3), `select`:** put this before `.py`:
```python
            elif rel.startswith(("docs/", ".github/", "benchmarks/", "posts/", "pdoc/")):
                s, r = set(), "docs"
```
Route binaries with `rel.startswith("tests/fixtures/")` to `self.fixture_change(f)` instead of
running everything. `sql_comment_only` is only tried for `.sql`, so this is safe.

**F8 (C5), `build_index.scan`:** for module and class-body calls, seed only
`isinstance(ch.func, ast.Name)` callees, or `Attribute` callees whose `.value` is `cls`/`self`.
For metaclasses, seed only methods *of that class*: record `(ch.name, method)` pairs instead of
bare `"__init__"`.

**F9 (C6), `def_names`:** when visiting a FunctionDef's children, skip nested FunctionDefs. Local
helpers cannot override anything. Change `visit(child, cls)` after `out.add(...)` to a visit that
ignores nested defs, or record depth and filter `depth == 0` per class or module.

## (d) Ranked recommendations

| # | Change | Impact |
|---|---|---|
| 1 | F1: nearest-recorded `exact_cov` + `dedents` guard | Closes the realistic slow-test misses (300 s each). Costs a few extra seconds only on dedenting patches. |
| 2 | F2: lost name imported or referenced by another sqlglot module → all | Stops suite-wide ImportError/AttributeError misses from deletion or rename patches. Cost only when it fires. |
| 3 | F3: `sqlglotc/` in `code_paths`, and its branch before `.py` | Closes a small miss. Saves a full run on sqlglotc `.py` edits. |
| 4 | F6: memoise `import_time_change`/`fixture_tests` | Cuts charged CPU on multi-line class-table edits (likely 0.1–1 s per such commit). |
| 5 | F4: index-missing fallback → all | Safety net only. No cost when the index exists. |
| 6 | F7, F8, F9: docs-dir `.py`, binary fixtures, narrower import_fns and new-def | Pure cost cuts. Expect fewer near-full runs on table and `join`-style edits. |
| 7 | F5, R6 | Edge-case recall. Cheap to do. |

The file kept changing during the review. Line numbers refer to the 834-line version.
