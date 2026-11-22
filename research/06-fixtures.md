# 06 - fixture -> test mapping

Scope: every file under `work/sqlglot/tests/fixtures` (static analysis only, nothing run). Test-time
numbers come from `sample/tests.json`, and match counts from `maps/impact.json.gz` `test_strings`.

## (a) Findings

### F1. Who actually reads each fixture (complete; checked by grep for `FIXTURES_DIR`, `load_sql_fixture*`, `open(`)
Only `tests/helpers.py:34-55` opens fixtures (`load_sql_fixtures`, `load_sql_fixture_pairs`), plus two direct
`open`s in `test_jsonpath.py:69` and `test_executor.py:36-44`. No test outside five modules touches fixtures.

| fixture | true readers (function-level ids) | evidence |
|---|---|---|
| `identity.sql` | `test_transpile::test_identity`, `test_serde::test_serde` | test_transpile.py:1050, test_serde.py:24 |
| `partial.sql` | `test_transpile::test_partial` | test_transpile.py:1098 |
| `pretty.sql` | `test_transpile::test_pretty` | test_transpile.py:1103 |
| `jsonpath/cts.json` | `test_jsonpath::test_cts_file` | test_jsonpath.py:69 |
| `jsonpath/LICENSE` | nobody | no reader |
| `optimizer/<name>.sql` | the one `test_optimizer` test whose `check_file("<name>", ...)` literal is `<name>` (table below) | test_optimizer.py:214-226 builds `f"optimizer/{file}.sql"` |
| `optimizer/annotate_types.sql` | `test_annotate_types` | test_optimizer.py:3667 (direct `load_sql_fixture_pairs`) |
| `optimizer/annotate_functions.sql` | `test_annotate_funcs` | test_optimizer.py:3955 |
| `optimizer/qualify_columns__invalid.sql` | `test_qualify_columns__invalid` | test_optimizer.py:2078 (`load_sql_fixtures`) |
| `optimizer/tpc-h/tpc-h.sql` | `test_optimizer::test_tpch` (3361), `test_executor::test_optimized_tpch` (158), `test_execute_tpch` (187); class-wide via `setUpClass` (test_executor.py:118) | |
| `optimizer/tpc-ds/tpc-ds.sql` | `test_optimizer::test_tpcds` (3364), `test_executor::test_execute_tpcds` (190); class-wide via `setUpClass` (119) | |
| `optimizer/tpc-*/<table>.csv.gz` (42 files) | every `TestExecutor` test, because `setUpClass` loads all CSVs into duckdb (test_executor.py:111-115); the data is only compared in the two `execute_tpc*` tests and `test_optimized_tpch` | |

`check_file` call sites -> test (all literal first args): optimize->`test_optimize`(optimizer.sql, 300);
isolate_table_selects (338); qualify_tables (408, test_qualify_tables); normalize (454, test_normalize);
qualify_columns + qualify_columns_ddl (998/1004, both `test_qualify_columns`);
qualify_columns__with_invisible (2069); pushdown_cte_alias_columns (2072, `test_pushdown_cte_alias_columns`);
normalize_identifiers (2143); quote_identifiers (2151); pushdown_projections (2157, `test_pushdown_projection`, singular);
simplify (2160); unnest_subqueries (2342); pushdown_predicates (2585); optimize_joins (2732);
eliminate_joins (2806); eliminate_ctes (2830); merge_subqueries (2929); eliminate_subqueries (3053);
canonicalize_internal_names (3062); canonicalize (3338); `"tpc-h/tpc-h"` (3361); `"tpc-ds/tpc-ds"` (3364).
Note the mismatches between fixture stem and test name that a name heuristic would miss:
`pushdown_projections`->`test_pushdown_projection`, `qualify_columns_ddl`->`test_qualify_columns`,
`optimizer`->`test_optimize`, `qualify_tables.sql` is read only by `test_qualify_tables`.
`optimizer/tpc-*` is read through the sub-path `"tpc-h/tpc-h"`, i.e. the dir name is part of the literal.

### F2. What `fixture_change` (selector.py:373-385) does on each file
`hits = tests with any string s where stem in s`. `test_strings` (tools/build_map.py:42-62) contains the
constants of the test body **plus every non-test helper method in the class plus all module-level
constants**. So `check_file`'s own constants (`"optimizer/"`, `"canonicalize_table_aliases"`,
`"validate_qualify_columns"`, `".sql"`) belong to every test in `TestOptimizer`, and the f-string pieces
`f"optimizer/{file}.sql"` land in the set as `"optimizer/"`, `".sql"`. Result (measured on the map):

| fixture | stem | tests picked | sample seconds | true set |
|---|---|---|---|---|
| `optimizer.sql` | `optimizer` | 231 (all TestOptimizer via `"optimizer/"`, all test_executor, 3 test_sqlglotc, test_dialect::test_coalesce) | 51.5 | 1 test (0.4 s) |
| `qualify_columns.sql` | `qualify_columns` | 167 (all TestOptimizer via `validate_qualify_columns`) | 19.8 | 1 test |
| `canonicalize.sql` | `canonicalize` | 167 (via `canonicalize_table_aliases`) | 19.8 | 1 test |
| `jsonpath/cts.json` | `cts` | 186 (substring of "facts", "objects", "contracts" ...) | 20.6 | 1 test (0.1 s) |
| `identity.sql` | `identity` | 5 (3 unrelated dialect tests) | 1.6 | 2 tests |
| `pretty.sql` | `pretty` | 2 (`test_parser::test_pretty_config_override` is unrelated) | tiny | 1 |
| `normalize.sql` | `normalize` | 2 (+`test_normalize_identifiers`) | tiny | 1 |
| `tpc-h.sql` | `tpc-h` | 61 (all TestExecutor + `test_tpch`) | 31.9 | 3 tests + class-level (see F4) |
| `tpc-ds.sql` | `tpc-ds` | 61 | 33.8 | 2 tests + class-level |
| the other 17 optimizer `.sql` | exact stem | 1-2 | | correct |

All "too many" cases are cost waste, not misses: the true readers are always inside the picked set.
`optimizer.sql` alone costs ~51 s of test time when picked (spread over 8 workers it is ~6.4 s plus the
2 s * workers penalty effect, and it drags in the executor TPC tests: `test_execute_tpcds` 21.5 s).

### F3. Misses (selector returns the wrong set or an empty one)
1. **Binary fixtures select nothing.** `parse_patch` (selector.py:62) sets `binary=True` on a `Binary files ... differ` line but
   never reads the `diff --git a/X b/X` header, and `old`/`new` stay `None` (only `---`/`+++` set them, 52-57). Line 75
   then sets `f["path"] = ""`. In `select` (393-394) `rel.startswith(("sqlglot/","tests/"))` is False for `""`, so a
   change to any `tests/fixtures/optimizer/tpc-*/*.csv.gz` returns `set()` ("binary-other"). Real failure set would be the
   whole `TestExecutor` class (setUpClass at test_executor.py:111-115 plus data compare at 168-173). With `git diff --binary`
   the path is also `""` and goes to "other" (all tests: safe but 103 s). No sample patch has a binary file, so the 0-miss
   score does not exercise this. This is the one outright recall hole in the fixture path.
2. **Renames/deletes keep only the new path.** `f["path"] = f["new"] or f["old"]` (selector.py:77). For a renamed or
   moved fixture (e.g. `identity.sql` -> `identity_base.sql` with a few hunks) the old name, which the tests still open,
   is never looked up, so stem misses the real readers (FileNotFoundError at the old reader).
   A header-only rename has `path == ""` -> "other" -> all tests (safe).
3. **csv.gz stem.** Even if the path were right, `Path.stem` of `customer.csv.gz` is `customer.csv`, which is in no test
   string; the parent-dir fallback (`tpc-h`) saves it only by luck of the module constants (it would pick the 61 tests).
4. **New fixture files** (`old is None`): stem hits nothing -> parent name `optimizer` -> 231 tests (51 s), or for a new
   top-level file the parent is `fixtures` -> all 1620. The reader is a *new or changed test* in the same patch, which
   `python_change`/`test_file_change` already selects, so selecting 231 or all is pure waste.
5. **Reader invisible to string match:** an f-string built name only keeps its literal fragments. Today every
   `check_file` call site passes a literal, so no live miss, but any new reader written as
   `check_file(name)` with `name` from a loop/tuple, or via `FIXTURES_DIR`/`os.path.join(..., "jsonpath", x)`, is
   invisible. This is a structural weakness of the stem rule that the static call-site map (below) removes.
6. `jsonpath/LICENSE`: no reader; stem `LICENSE` has no hit; parent `jsonpath` matches whichever tests mention
   "jsonpath" (dozens of dialect tests). Cost only.

### F4. Class-level (setUpClass) readers
`TestExecutor.setUpClass` parses both TPC `.sql` files (118-119) and loads all 42 CSVs. If the loader chokes
(`_extract_meta` helpers.py:15-24 does `line.split(":", 1)` on every `#` line, so a `#` line without a colon raises
ValueError), every `TestExecutor` test errors, not just the tpc ones. Content edits that keep the structure
(SQL text, expected output) only change `test_tpch`/`test_tpcds`/`test_optimized_tpch`/`test_execute_tpch(s)`.

### F5. Comment-only rule (selector.py:376, 427-431) vs `tests/helpers.py:11-12`
`_filter_comments` keeps `line and not line.startswith("--")` on `s.splitlines()`. The selector's predicate
(`t == "" or t.startswith("--")`) is at least as strict as the loader's, so it is **sound** for all readers:
- `load_sql_fixtures` (34-41) and `load_sql_fixture_pairs` (44-55) both call `_filter_comments` first; there is no
  other `.sql` reader in the repo (tests/gen_fixtures.py only writes). The `;` split (47) and `#` meta parsing happen after the filter, so a
  `--` line containing `;` or `#` is inert too. Test titles use the enumerate index of *statements*, which is unchanged.
- Whitespace-only lines (`"  "`) and indented comments (`"  -- x"`) are NOT filtered by the loader and are NOT
  matched by the selector (text is `line[1:]`, 66, exact) -> treated as real changes. Correct (conservative).
- CRLF: a blank CRLF line arrives as `"\r"` -> not inert. Conservative.
- The rule is limited to `.sql`; `cts.json` and csv have no comment concept. Correct.
- Patch-text pitfall (not a bug): a removed `-- x` line appears as `--- x` in the diff body; `parse_patch` only treats
  `--- ` as a header while `not cur["hunks"]` (selector.py:55), so it is handled.
- Pure new/deleted files are excluded by `f["old"] and f["new"]`. Good.

### F6. Helper indirection in `tests/helpers.py`
`test_file_change` (selector.py:341-345) returns **all tests** for any edit to `tests/helpers.py` because it has no tests.
Consumers (grep `tests.helpers`): test_executor (FIXTURES_DIR, SKIP_INTEGRATION, TPCH/TPCDS_SCHEMA, load_sql_fixture_pairs),
test_optimizer (TPCDS/TPCH_SCHEMA, `_extract_meta`, assert_logger_contains, both loaders, string_to_bool),
test_transpile (assert_logger_contains, both loaders), test_serde (is_compiled, load_sql_fixtures),
test_jsonpath (FIXTURES_DIR), test_parser (assert_logger_contains), test_generator and dialects/test_dialect
(`is_compiled`, used at module level, so the whole module).

## (b) Recall risks and cost waste
Recall: (R1) binary csv.gz -> empty set (F3.1); (R2) rename/move of a fixture only checks the new name (F3.2);
(R3) a future reader that builds names dynamically is invisible to stem (F3.5; none today).
Waste (per patch touching the fixture, sample seconds): `optimizer.sql` 51.5 s; `tpc-h.sql` 31.9 s and `tpc-ds.sql` 33.8 s
(the 21.5 s `test_execute_tpcds` is legitimately needed only for tpc-ds); `qualify_columns.sql` 19.8 s;
`canonicalize.sql` 19.8 s; `cts.json` 20.6 s; every `tests/helpers.py` edit = all 103 s; new fixture file 51-103 s.
Any one of these costs roughly 6-13 s of makespan on 8 workers; the map below makes each a 1-3 test selection.

## (c) Proposed changes

### c1. tools/build_map.py: exact fixture map by static analysis (no tuning to answers)
Add next to `test_strings`, call from `main()` and put the result in the payload as `"fixtures"`.
Idea: evaluate path expressions that reach `FIXTURES_DIR` / the loaders, resolve helper calls one level
(`check_file(file, ...)`), attribute the result to the enclosing test (or the whole class for `setUpClass`/`setUp`,
the whole module for module level), and expand wildcards against the real file list.

```python
import fnmatch

FIX = "tests/fixtures"

def _ev(node, env):
    """Constant-fold a path expression; unresolved parts become '*'."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return env.get(node.id, "*") if node.id != "FIXTURES_DIR" else FIX
    if isinstance(node, ast.JoinedStr):
        return "".join(_ev(v.value, env) if isinstance(v, ast.FormattedValue) else v.value for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _ev(node.left, env) + _ev(node.right, env)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "join"):                      # os.path.join
        return "/".join(_ev(a, env) for a in node.args)
    return "*"

def fixture_map(test_ids_index, all_fixtures):
    """fixture rel path -> sorted test indices; plus 'weak' class-level readers."""
    loaders = {"load_sql_fixtures", "load_sql_fixture_pairs"}        # asserted below against helpers.py
    helpers = ast.parse((ROOT / "tests/helpers.py").read_text())
    for n in helpers.body:
        if isinstance(n, ast.FunctionDef) and any(isinstance(x, ast.Name) and x.id == "FIXTURES_DIR" for x in ast.walk(n)):
            loaders.add(n.name)
    strong: dict[str, set[int]] = {}
    weak: dict[str, set[int]] = {}

    def add(table, pattern, ids):
        pattern = pattern.replace("//", "/")
        for fx in all_fixtures:
            if fnmatch.fnmatchcase(fx, pattern) or fnmatch.fnmatchcase(fx, FIX + "/" + pattern):
                table.setdefault(fx, set()).update(ids)

    for path in sorted((ROOT / "tests").glob("test_*.py")) + sorted((ROOT / "tests/dialects").glob("test_*.py")):
        rel = str(path.relative_to(ROOT))
        tree = ast.parse(path.read_text())
        env = {t.targets[0].id: _ev(t.value, {}) for t in tree.body
               if isinstance(t, ast.Assign) and isinstance(t.targets[0], ast.Name)}
        module_ids = {i for t, i in test_ids_index.items() if t.startswith(rel + "::")}
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
            methods = {m.name: m for m in cls.body if isinstance(m, ast.FunctionDef)}
            class_ids = {i for t, i in test_ids_index.items() if t.startswith(f"{rel}::{cls.name}::")}

            def reads(fn, env2):        # [(pattern)] of loader/open calls in fn, resolved with env2
                out = []
                for c in ast.walk(fn):
                    if not isinstance(c, ast.Call):
                        continue
                    name = c.func.id if isinstance(c.func, ast.Name) else getattr(c.func, "attr", "")
                    if name in loaders and c.args:
                        out.append(FIX + "/" + _ev(c.args[0], env2))
                    elif name == "open" and c.args:
                        out.append(_ev(c.args[0], env2))
                return out

            # helper methods that read fixtures through a parameter, e.g. check_file(self, file, ...)
            helper_reads = {n: m for n, m in methods.items()
                            if not n.startswith("test") and n not in ("setUp", "setUpClass", "tearDownClass") and reads(m, {})}
            for name, fn in methods.items():
                if name in helper_reads:
                    continue
                tid = f"{rel}::{cls.name}::{name}"
                ids = {test_ids_index[tid]} if tid in test_ids_index else class_ids
                table = strong if tid in test_ids_index else weak       # setUp/setUpClass = class-wide
                for pat in reads(fn, env):
                    add(table, pat, ids)
                for c in ast.walk(fn):                                  # self.check_file("x", ...)
                    if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr in helper_reads:
                        h = helper_reads[c.func.attr]
                        params = [a.arg for a in h.args.args][1:]       # drop self
                        env2 = dict(env)
                        for p, a in zip(params, c.args):
                            env2[p] = _ev(a, env)
                        for k in c.keywords:
                            if k.arg in params:
                                env2[k.arg] = _ev(k.value, env)
                        for pat in reads(h, env2):
                            add(table, pat, ids)
        # module-level reads (import time) -> every test in the module
        for node in tree.body:
            if not isinstance(node, (ast.ClassDef, ast.FunctionDef)):
                for pat in reads(node, env) if False else []:
                    pass
    return strong, weak
```
(Tidy before use: drop the empty module-level stub or implement it with the same `reads` closure moved to module
scope; there is no module-level fixture read today, `is_compiled` is the only module-level helper use.) Plain
module-level test functions (`def test_x` outside a class) need the same treatment as methods.

Resulting entries (what this yields on the pin, derived, not hand-written): `optimizer/qualify_columns.sql` ->
{`test_qualify_columns`}; `qualify_columns_ddl.sql` -> {same}; `optimizer.sql` -> {`test_optimize`};
`pushdown_projections.sql` -> {`test_pushdown_projection`}; `identity.sql` -> {test_identity, test_serde};
`cts.json` -> {`test_cts_file`}; `tpc-h.sql` strong {`test_tpch`, `test_optimized_tpch`, `test_execute_tpch`}, weak = all
`TestExecutor` (setUpClass); `tpc-ds.sql` strong {`test_tpcds`, `test_execute_tpcds`}, weak = class; csv.gz
-> weak = class (the open() is inside the nested `setup`, reached from setUpClass). `LICENSE` -> [] and is listed,
so "known but unread" is distinguishable from "unknown file". The csv wildcard works because `directory`
is an unresolved parameter -> `*`, matching both tpc dirs conservatively.

Payload: `"fixtures": {"<rel>": {"strong": [idx], "weak": [idx]}}` for every file in `tests/fixtures`.

Also build a per-helper-name map for `tests/helpers.py` (c3).

### c2. selector.py: use the map; fix the paths (replace `fixture_change`, and patch `parse_patch`)
```python
# parse_patch: after the 'diff --git ' branch creates cur, record the header paths so binary/rename files have a path
        if line.startswith("diff --git "):
            cur = {"old": None, "new": None, "hunks": [], "binary": False}
            a, _, b = line[len("diff --git "):].partition(" b/")
            cur["git_old"], cur["git_new"] = a[2:] if a.startswith("a/") else a, b
            files.append(cur)
            continue
# end of parse_patch, replace the empty-path fallback:
    for f in files:
        if f["old"] is None and f["new"] is None:
            f["old"], f["new"] = f.get("git_old") or "", f.get("git_new") or ""
        f["path"] = f["new"] or f["old"]
```
(Paths containing " b/" are not present under sqlglot; for safety keep the existing `---/+++` priority.)
In `select`, handle binary fixtures by path before the generic binary rule:
```python
            if f["binary"] and not rel.startswith("tests/fixtures/"):
                s, r = (set(self.all), "binary") if rel.startswith(("sqlglot/", "tests/")) else (set(), "binary-other")
            elif ... (existing) ...
```
and new `fixture_change` (uses `self.index["fixtures"]`, loaded where `self.index`/map payload is):
```python
    def fixture_change(self, f: dict) -> tuple[set[int], str]:
        fx = self.fixtures                                  # payload["fixtures"]
        names = [p for p in dict.fromkeys((f["old"], f["new"])) if p]   # rename: both names
        if (not f["binary"] and Path(f["path"]).suffix == ".sql" and f["old"] and f["new"]
                and f["old"] == f["new"] and sql_comment_only(f)):
            return set(), "fixture-comment"
        known = [n for n in names if n in fx]
        if known:
            out: set[int] = set()
            structural = f["binary"] or not (f["old"] and f["new"]) or f["old"] != f["new"] or touches_meta(f)
            for n in known:
                out |= set(fx[n]["strong"])
                if structural:                              # setUpClass readers only break on structure/binary edits
                    out |= set(fx[n]["weak"])
            return out, "fixture-map"
        # unknown file (new fixture or unmapped): the readers are loader modules; the reader test is in the same patch
        return set(self.fixture_modules), "fixture-new"     # all tests in test_optimizer/transpile/serde/executor/jsonpath (~53 s)
```
with
```python
def touches_meta(f):                       # a '#key: val' meta line or a statement terminator layout change
    return any(t.startswith("#") for h in f["hunks"] for tag, t, _ in h["lines"] if tag in "+-")
```
Use `fixture_modules` only if safe; cheaper variant: `fixture-new` selects tests of those modules *minus* TestExecutor
unless a csv/tpc file (executor reads only by directory, already in the map): `set().union(by_file[m] for m in
("tests/test_optimizer.py","tests/test_transpile.py","tests/test_serde.py","tests/test_jsonpath.py"))` = 206 tests, 21.8 s.
For a brand-new file, a hand-written reader exists only in the same patch's `.py` change, already selected, so even
`set()` is defensible; I recommend the 206-test version as the miss insurance.
Note `csv.gz` always counts as structural, giving the whole `TestExecutor` class (60 tests, 31.6 s). If that is too
costly, `strong` for a tpc-h csv could be restricted to {`test_optimized_tpch`,`test_execute_tpch`} but a malformed gz
kills `setUpClass` for everything, so keep the class.

### c3. selector.py: `tests/helpers.py` edits by name (instead of all tests)
In `test_file_change`, for `rel == "tests/helpers.py"`, take the touched old lines, find the enclosing top-level
def/assign in `ast.parse(old)`, and use a map built in build_map from `ImportFrom(module="tests.helpers")` plus
`ast.Name` use per test: `{helper_name: [test idx]}` (a helper used by `is_compiled` at module level -> all tests of that
module). Loader edits (`_filter_comments`, `_extract_meta`, `load_*`) resolve to the union of loader users automatically
(transitively: map `_filter_comments` -> users of `load_*`). If a touched line is outside any def/assign (imports,
`FIXTURES_DIR`), take all users of any helper name. Expected: a comment/docstring-only or `TPCH_SCHEMA` edit goes from 1620 to
~30-230 tests; `string_to_bool` (used by `SKIP_INTEGRATION` at module import) must stay "all importers of helpers".
Because every `import` of helpers happens at module load, never go below "all tests of an importing module" for
`FIXTURES_DIR`, `SKIP_INTEGRATION`, `is_compiled`, anything used at module level.

### c4. selector.py: tighten the stem fallback (if the map is not adopted)
Cheap stopgap that fixes the biggest waste without new data: match the stem only against *test-body*
constants of the test itself, not the helper/module constants. In `tools/build_index.py`/`build_map.py::test_strings`,
store `own = consts(item)` separately from `helper|module`; `fixture_change` uses `own` for `stem`. That alone shrinks
`optimizer.sql`, `qualify_columns.sql`, `canonicalize.sql` (check_file's `canonicalize_table_aliases` is helper-only) to the one
test whose body contains the literal, and `cts.json` to `test_cts_file` (its body has `"cts.json"`). It needs the
dir-stem rule `"tpc-h/tpc-h"` -> `stem in s` with the parent joined (`f"{parent}/{stem}"`) to stay correct for the
tpc files; the executor tests need the `weak` class rule, so the map (c1) is still the correct fix.

## (d) Ranked recommendations
1. **Fix the binary/rename path bug** (c2 `parse_patch` header paths + route binary fixtures through the fixture rule).
   It is the only fixture path that can produce a 300 s miss (empty set on a changed `*.csv.gz`). Cost: zero. Do first.
2. **Look up both old and new names for renames/deletes** (c2). Prevents a second small miss class.
3. **Static fixture map in `build_map.py` (c1) + `fixture_change` rewrite (c2).** Replaces substring guessing with the
   real reader set; cuts `optimizer.sql` 51.5 s -> 0.4 s, `qualify_columns.sql`/`canonicalize.sql`/`cts.json` ~20 s -> <1 s,
   `tpc-h.sql` 31.9 s -> ~0.8 s (strong) with the class only on structural edits. Impact is per fixture-touching patch;
   the sample's fixture patches mostly hit unambiguous stems so the sampled gain is small, but the hidden set's `optimizer.sql`,
   `qualify_columns.sql`, `canonicalize.sql` patches would each save ~5-8 s of makespan.
4. **`tests/helpers.py` by-name selection (c3).** Avoids all-1620 on helper edits; moderate payoff, low frequency.
5. **Own-body stem strings (c4)** only if the map is not shipped; otherwise skip.
6. Keep the comment-only rule as is (sound against `_filter_comments`, F5). Optionally restrict it to `old == new` (rename guard,
   included in c2) so a rename of a `.sql` file with comment changes does not look inert.
