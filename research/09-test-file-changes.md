# 09: Patches that edit tests/*.py

Scope: `Selector.test_file_change` and the test-file branches of `python_change` (selector.py line numbers are from 2026-11-21; the file is being edited, so diffs below are anchored on function names). Nothing here was run.

## (a) Findings

1. **The cross-file test import graph is small, and it all passes through two hubs** (from grep over `work/sqlglot/tests`):
   - `tests/dialects/test_dialect.py:27-117` defines `class Validator(unittest.TestCase)`, with `parse_one` (30), `assert_duckdb_sql` (33), `validate_identity` (54), `validate_transpile` (69), `validate_all` (75) and the class attribute `dialect = None` (28). **33 files** run `from tests.dialects.test_dialect import Validator`: every `tests/dialects/test_*.py` except test_dialect.py itself. Every class in them subclasses `Validator` directly. No other test base classes or mixins exist; the only other non-TestCase classes are `FakeDialect` (test_dialect_entry_points.py:8) and a local `CustomExpression` (test_serde.py:16).
   - `tests/helpers.py` is imported by test_parser.py:9, test_optimizer.py:26, test_generator.py:11, test_jsonpath.py:8, test_serde.py:7, test_executor.py:22, test_transpile.py:9 and **test_dialect.py:20** (`is_compiled`), so it reaches every dialect test as well. Who uses which name: `is_compiled` (generator, serde, dialect); `assert_logger_contains` (parser, optimizer, transpile); `load_sql_fixtures` (optimizer, serde, transpile); `load_sql_fixture_pairs` (executor, transpile, optimizer); `TPCH_SCHEMA`/`TPCDS_SCHEMA` (optimizer, executor); `FIXTURES_DIR` (jsonpath, executor); `SKIP_INTEGRATION` (executor); `string_to_bool` (optimizer). `_filter_comments` and `_extract_meta` are used only inside helpers.py, by the loaders.
   - test_dialect.py does real work at import time: `_PARSER_IS_COMPILED = is_compiled(_base_module)` (24), which is read by the decorators `@unittest.skipIf(_PARSER_IS_COMPILED, ...)` at 6100 and 6120. Imports at 5-22 include `WS_CONTROL_CHARS_TO_DUCK` (16, defined at sqlglot/generators/duckdb.py:68 and re-exported) and `SnowflakeParser` (19). If any of these raises, all 34 dialect test files fail to import.
2. **Class-level fixtures:** `setUpClass` at test_executor.py:73 (plus `tearDownClass` at 122), test_optimizer.py:110, test_lineage.py:14 and test_sqlglotc.py:15. `setUp` at test_optimizer.py:157, test_sqlglotc.py:29 and test_dialect_entry_points.py:13. None of these classes has subclasses, so the scope of each is a single class.
3. **Shared helpers inside one file:** module-level functions at test_optimizer.py:37-97, test_executor.py:34-57 (`mp_execute` and `initializer` are passed to a process pool), test_diff.py:7,11 and test_typing.py:10-40. Class helpers: `check_file` (test_optimizer.py:214), `cached_execute`/`rename_anonymous`/`subtestHelper`/`_mp_execute` (test_executor.py:126-175), test_schema.py:38-49, test_transpile.py:21, test_transforms.py:19, test_anonymize.py:8, test_serde.py:20, test_diff.py:401 and `_validate_array_sort` (test_dialect.py:1535).
4. **What `test_file_change` does today** (selector.py:405-428). It maps each touched old line to a `test*` def. **Any other line sets `whole=True` and returns only `self.by_file[rel]`.** If the file has no tests in tests.json (helpers.py, leakcheck.py, gen_fixtures.py, test_docs.py, test_integration_loader.py and the `__init__.py` files), it returns `self.all` (407-409). A syntax error in a test file returns `by_file[rel]` only (selector.py:333-334).
5. The coverage map holds `sqlglot/` lines only (build_map.py:96 `if not rel.startswith("sqlglot/")`), so nothing about test code can be read from it. Test-to-test dependencies need a static graph.
6. Sample size: 1020 of the 1620 tests (10.45 s) are in tests/dialects/, and 113 of them are in test_dialect.py. None of the 30 sample patches edits a tests/*.py file, so the sample cannot reveal any of the risks below.

## (b) Recall risks (misses) and waste under the current selector

| # | Patch shape | Current result | Truth |
|---|---|---|---|
| R1 | Edit to a `Validator` method body, e.g. `validate_all` (test_dialect.py:75-117) | 113 tests of test_dialect.py | Every subclass test that calls it: about 1000 tests in 34 files. **Miss.** |
| R2 | Edit to `Validator.dialect` (28) or to a class-level attribute | same | Every Validator subclass. **Miss.** |
| R3 | Module-level edit in test_dialect.py: imports 5-22, line 24 | same | ImportError breaks all 34 files. **Miss.** |
| R4 | Syntax error in test_dialect.py | `by_file` (333-334) | All 34 files. **Miss.** |
| R5 | test_dialect.py deleted | `whole` → `by_file` | All 34 files. **Miss.** |
| R6 | A new method added to `Validator` that shadows a TestCase hook (`setUp`, `run`, `subTest`) | the neighbours' lines → test_dialect.py | Every subclass. **Miss.** |
| R7 | (sqlglot side) A module-level name that a test imports is removed or renamed, e.g. `WS_CONTROL_CHARS_TO_DUCK` | `import_time_change`: duckdb family coverage plus tests naming "duckdb" | Import error in all 34 dialect files. A test such as test_athena that never mentions duckdb is a **likely miss**: import-time lines carry 4 tests at most (checked in maps: duckdb.py lines 1-25 → 4 tests each). |

Waste:
- **W1:** any helpers.py edit selects `self.all`, about 103 s, including test_max_nodes (28.8 s) and execute_tpcds (21.5 s). An `assert_logger_contains` body edit needs only parser, optimizer and transpile.
- **W2:** an edit to a helper or setUp selects the whole file. For example, `rename_anonymous` in test_executor drags in execute_tpcds (21.5 s).
- **W3:** a new test file selects `self.all`, although its own tests are not in this run.
- **W4:** inserting a test between two methods touches the blank line between them, which sets `whole=True` and selects the whole file.
- **W5:** an edit to tests/sqlglot/__init__.py selects everything, although that package holds no tests in the run.

## (c) Proposed code

### c1. `tools/build_index.py`: static test graph, written to its own file and loaded only when a test file changes

Add `import unittest`. Put the following above `main()`:
```python
def _reads(nodes) -> set[str]:
    """Names, attribute names and identifier-like strings (getattr(self, "x")) read under nodes."""
    out: set[str] = set()
    for node in nodes:
        for n in ast.walk(node):
            if isinstance(n, ast.Name):
                out.add(n.id)
            elif isinstance(n, ast.Attribute):
                out.add(n.attr)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
                out.add(n.value)
    return out

def _def_time(fn) -> list:
    """Parts of a def evaluated at import: decorators, defaults, annotations."""
    a = fn.args
    parts = list(fn.decorator_list) + list(a.defaults) + [d for d in a.kw_defaults if d is not None]
    parts += [x.annotation for x in a.posonlyargs + a.args + a.kwonlyargs if x.annotation]
    return parts + ([fn.returns] if fn.returns else [])

FN = (ast.FunctionDef, ast.AsyncFunctionDef)

def build_tests(root: Path) -> dict:
    trees = {}
    for p in sorted((root / "tests").rglob("*.py")):
        if "__pycache__" not in p.parts:
            try:
                trees[str(p.relative_to(root))] = ast.parse(p.read_text(encoding="utf-8"))
            except Exception:
                continue
    defined: set[str] = set()  # every module/class-level binding in test code; reads are filtered to these
    for tree in trees.values():
        for node in tree.body:
            if isinstance(node, (*FN, ast.ClassDef)):
                defined.add(node.name)
                if isinstance(node, ast.ClassDef):
                    defined.update(b.name for b in node.body if isinstance(b, FN))
            else:
                defined.update(n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))
                defined.update((a.asname or a.name).split(".")[0] for n in ast.walk(node) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names)
    keep = lambda s: sorted(s & defined)
    files, importers, classes, subclasses = {}, {}, {}, {}
    for rel, tree in trees.items():
        origin = {}  # local name -> "file::Name" for names imported from test modules
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if node.level:
                    anchor = str(Path(rel).parent).split("/")
                    mod = ".".join(anchor[: len(anchor) - (node.level - 1)] + ([mod] if mod else []))
                if mod != "tests" and not mod.startswith("tests."):
                    continue
                for a in node.names:
                    local = a.asname or a.name
                    if (sub := module_file(f"{mod}.{a.name}", root)):  # `from tests import helpers`
                        importers.setdefault(sub, []).append([rel, None, local])
                    elif (dep := module_file(mod, root)):
                        importers.setdefault(dep, []).append([rel, a.name, local])
                        origin[local] = f"{dep}::{a.name}"
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] == "tests" and (dep := module_file(a.name, root)):
                        importers.setdefault(dep, []).append([rel, None, a.asname or "tests"])
        units = []  # [kind, class, name, body_reads, import_time_reads]
        for node in tree.body:
            if isinstance(node, FN):
                units.append(["def", None, node.name, keep(_reads(node.body)), keep(_reads(_def_time(node)))])
            elif isinstance(node, ast.ClassDef):
                key, meths = f"{rel}::{node.name}", {}
                head = [b for b in node.body if not isinstance(b, FN)]
                units.append(["head", node.name, None, [], keep(_reads(node.decorator_list + node.keywords + head))])
                for b in node.body:
                    if isinstance(b, FN):
                        meths[b.name] = keep(_reads(b.body))
                        units.append(["meth", node.name, b.name, meths[b.name], keep(_reads(_def_time(b)))])
                bases = [origin.get(n, f"{rel}::{n}") for n in
                         (b.id if isinstance(b, ast.Name) else getattr(b, "attr", None) for b in node.bases) if n]
                classes[key] = {"bases": bases, "methods": meths}
                for b in bases:
                    subclasses.setdefault(b, []).append(key)
            else:
                units.append(["mod", None, None, [], keep(_reads([node]))])
        files[rel] = units
    hooks = set(dir(unittest.TestCase)) | {"setUpClass", "tearDownClass", "setUpModule", "tearDownModule", "load_tests"}
    return {"files": files, "importers": importers, "classes": classes, "subclasses": subclasses, "hooks": sorted(hooks)}
```
In `main()`, after `out.write_text(...)`, add the following. A failure here must not fail the build:
```python
    try:
        (out.parent / "test_index.json").write_text(json.dumps(build_tests(root), separators=(",", ":")))
    except Exception as e:
        print(f"build_tests failed: {type(e).__name__}: {e}", file=sys.stderr)
```

### c2. `selector.py`: module-level helpers (next to `def_names`)
```python
TEST_INDEX = Path(os.environ.get("TEST_INDEX", APP / "maps/test_index.json"))
SAFE_DECO_ROOTS = {"unittest", "mock", "patch", "pytest"}
FN = (ast.FunctionDef, ast.AsyncFunctionDef)

def _def_time(fn) -> list:  # same as in build_index.py
    a = fn.args
    parts = list(fn.decorator_list) + list(a.defaults) + [d for d in a.kw_defaults if d is not None]
    parts += [x.annotation for x in a.posonlyargs + a.args + a.kwonlyargs if x.annotation]
    return parts + ([fn.returns] if fn.returns else [])

def _safe_def_time(parts) -> bool:
    """unittest.skip("x")-style decorators: cannot raise at import."""
    return all((isinstance(n, ast.Name) and n.id in SAFE_DECO_ROOTS) or
               isinstance(n, (ast.Call, ast.Attribute, ast.Constant, ast.keyword, ast.expr_context))
               for p in parts for n in ast.walk(p))

def _bound(stmt) -> set[str]:
    if isinstance(stmt, (*FN, ast.ClassDef)):
        return {stmt.name}
    return ({(a.asname or a.name).split(".")[0] for n in ast.walk(stmt) if isinstance(n, (ast.Import, ast.ImportFrom))
             for a in n.names if a.name != "*"}
            | {n.id for n in ast.walk(stmt) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)})

def _pure(stmt) -> bool:
    """Runs at import but can neither raise nor read anything: a docstring, `pass`, or NAME = literal."""
    if isinstance(stmt, ast.Pass) or (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)):
        return True
    if not (isinstance(stmt, ast.Assign) and all(isinstance(t, ast.Name) for t in stmt.targets)):
        return False
    try:
        ast.literal_eval(stmt.value)
        return True
    except Exception:
        return False

def _head(cls) -> list:
    return [b for b in cls.body if not isinstance(b, FN)]

def test_units(tree) -> dict:
    """(kind, class, name|dump) -> (comparison key, node). ast.dump ignores line numbers."""
    out = {}
    for node in tree.body:
        if isinstance(node, FN):
            out[("def", None, node.name)] = (ast.dump(node), node)
        elif isinstance(node, ast.ClassDef):
            key = "|".join(ast.dump(x) for x in node.bases + node.keywords + node.decorator_list + _head(node))
            out[("head", node.name, None)] = (key, node)
            for b in node.body:
                if isinstance(b, FN):
                    out[("meth", node.name, b.name)] = (ast.dump(b), b)
        else:
            d = ast.dump(node)
            out[("mod", None, d)] = (d, node)
    return out
```

### c3. `selector.py`: Selector methods

In `__init__`, add:
```python
        self._tindex = None
        self.tests_by_class: dict[str, dict[str, set[int]]] = {}
        for t in tests:
            p = t["id"].split("::")
            if len(p) == 3:
                self.tests_by_class.setdefault(f"{p[0]}::{p[1]}", {}).setdefault(p[2].split("[")[0], set()).add(t["number"])
```
Then replace `test_file_change` with the following:
```python
    def test_index(self) -> dict:
        if self._tindex is None:
            try:
                self._tindex = json.loads(TEST_INDEX.read_text())
            except Exception as e:
                log(f"no test index ({type(e).__name__})")
                self._tindex = {}
        return self._tindex

    def file_closure(self, rels: set[str]) -> set[int]:
        """Every test in these files and in every test file that imports them, transitively."""
        imp = self.test_index().get("importers", {})
        seen, todo = set(), list(rels)
        while todo:
            r = todo.pop()
            if r not in seen:
                seen.add(r)
                todo.extend(e[0] for e in imp.get(r, []))
        return set().union(*(self.by_file.get(r, set()) for r in seen))

    def _descendants(self, roots) -> set[str]:
        subs, seen, todo = self.test_index()["subclasses"], set(), list(roots)
        while todo:
            x = todo.pop()
            if x not in seen:
                seen.add(x)
                todo.extend(subs.get(x, []))
        return seen

    def class_scope(self, roots) -> set[int]:
        return set().union(*(n for x in self._descendants(roots) for n in self.tests_by_class.get(x, {}).values()))

    def class_closure(self, roots, start: set[str]) -> set[int]:
        """Tests in roots and their subclasses whose self./name call closure, resolved over
        the class's bases, reaches a name in start. Ignores overrides, so it can only over-select."""
        classes, out = self.test_index()["classes"], set()
        for x in self._descendants(roots):
            chain, q = [], [x]
            while q:
                k = q.pop(0)
                if k in classes and k not in chain:
                    chain.append(k)
                    q.extend(classes[k]["bases"])
            reach, grew = set(start), True
            while grew:
                grew = False
                for k in chain:
                    for m, uses in classes[k]["methods"].items():
                        if m not in reach and not reach.isdisjoint(uses):
                            reach.add(m)
                            grew = True
            for name, nums in self.tests_by_class.get(x, {}).items():
                if name in reach:
                    out |= nums
        return out

    def name_closure(self, rel: str, names: set[str]):
        """Spread module-level names through `from tests.x import n` and module functions.
        Returns the dirty names per file and the files that read one at import time."""
        TI, dirty, import_time = self.test_index(), {}, set()
        work = [(rel, n) for n in names]
        while work:
            fl, n = work.pop()
            if n in dirty.setdefault(fl, set()):
                continue
            dirty[fl].add(n)
            for imp, name, local in TI["importers"].get(fl, []):
                if name == n:
                    work.append((imp, local))
                elif name in (None, "*"):
                    work.append((imp, n))
            for kind, _cls, uname, body, deftime in TI["files"].get(fl, []):
                if n in deftime:
                    import_time.add(fl)  # a module statement, class body or decorator: may raise at import
                elif kind == "def" and n in body:
                    work.append((fl, uname))
        return dirty, import_time

    def test_file_change(self, f: dict, old_tree, new_tree) -> tuple[set[int], str]:
        rel, TI = f["old"] or f["new"], self.test_index()
        if not TI:
            return set(self.all), "test-file-no-index"
        if Path(rel).name in ("__init__.py", "conftest.py"):
            d = str(Path(rel).parent) + "/"
            return self.file_closure({x for x in set(TI["files"]) | set(self.by_file) if x.startswith(d)}), "test-package"
        if old_tree is None:
            return set(), "test-file-added"  # its own tests are not in this run; edited importers come as their own files
        if new_tree is None:
            return self.file_closure({rel}), "test-file-deleted"
        old_u, new_u = test_units(old_tree), test_units(new_tree)
        old_b = set().union(*(_bound(n) for n in old_tree.body))
        new_b = set().union(*(_bound(n) for n in new_tree.body))
        picked: set[int] = set()
        if gone := old_b - new_b:  # `from rel import gone` now raises in the importer
            picked |= self.file_closure({e[0] for e in TI["importers"].get(rel, []) if e[1] in gone or e[1] == "*"})
        names, methods, scopes = old_b ^ new_b, set(), set()
        for key in old_u.keys() | new_u.keys():
            o, n = old_u.get(key), new_u.get(key)
            if o and n and o[0] == n[0]:
                continue
            kind, cls, name = key
            if kind == "mod":
                if not _pure((n or o)[1]):
                    return self.file_closure({rel}), "test-module-level"
                names |= _bound((n or o)[1])
            elif kind == "head":
                if o and n:
                    sig = lambda c: [ast.dump(x) for x in c.bases + c.keywords + c.decorator_list]
                    if sig(o[1]) != sig(n[1]) or not all(_pure(s) for s in _head(n[1])):
                        return self.file_closure({rel}), "test-class-head"
                scopes.add(cls)  # class attributes (dialect, maxDiff) reach every test of the class and its subclasses
            else:
                dt_o = [ast.dump(x) for x in _def_time(o[1])] if o else []
                dt_n = [ast.dump(x) for x in _def_time(n[1])] if n else []
                if dt_o != dt_n and n and not _safe_def_time(_def_time(n[1])):
                    return self.file_closure({rel}), "test-decorator"  # decorators run at import
                if kind == "def":
                    if name in TI["hooks"]:  # setUpModule, load_tests
                        return self.file_closure({rel}), "test-module-hook"
                    names.add(name)  # test functions are picked below via by_id
                elif name in TI["hooks"] or name.startswith("__"):
                    scopes.add(cls)  # setUp, setUpClass, tearDown*, run, assert* overrides
                else:
                    methods.add((cls, name))
        dirty, import_time = self.name_closure(rel, names)
        picked |= self.file_closure(import_time)
        for fl, ns in dirty.items():
            picked |= {self.by_id[f"{fl}::{x}"] for x in ns if f"{fl}::{x}" in self.by_id}
        if dirty:
            roots = {k for k in TI["classes"] if k.split("::")[0] in dirty}
            picked |= self.class_closure(roots, set().union(*dirty.values()))
        for cls, m in methods:
            hit = self.class_closure({f"{rel}::{cls}"}, {m})
            if not hit and not m.startswith("test"):
                hit = self.class_scope({f"{rel}::{cls}"})  # helper with no static caller: whole class tree
            picked |= hit
        return picked | self.class_scope({f"{rel}::{c}" for c in scopes}), "test-file"
```
Behaviour of `test_file_change` above:
- A test body edit selects that test, plus the same-named inherited test in subclasses.
- A helper edit selects the tests whose closure reaches it, across files.
- An edit to a hook, `setUp*` or a class attribute selects the class tree.
- Module-level, decorator or syntax changes select the file plus its transitive importers.

### c4. `selector.py`: `python_change`
```diff
-            if rel.startswith("tests/") and rel in self.by_file:
-                return set(self.by_file[rel]), "syntax-error-test"
+            if rel.startswith("tests/") and self.test_index():
+                return self.file_closure({f["old"] or rel}), "syntax-error-test"  # the file and its importers
             return set(self.all), "syntax-error"
 ...
         if rel.startswith("tests/"):
-            return self.test_file_change(f, old), "test-file"
+            return self.test_file_change(f, old_tree, new_tree)
```

### c5. (R7) sqlglot names imported by test modules
In `build_tests`, also record `sq[name] = [test files]` for every `from sqlglot... import name` (asname-independent), and return it as `"sqlglot_names"`. In `sqlglot_change`, after parsing, add:
```python
        if rel.startswith("sqlglot/") and (TI := self.test_index()):
            lost = set().union(*map(_bound, old_tree.body)) - set().union(*map(_bound, new_tree.body))
            users = {t for n in lost for t in TI.get("sqlglot_names", {}).get(n, [])}
            out |= self.file_closure(users)  # the test module now fails to import
```
This is keyed by name, not by module. Re-exports (`dialects/duckdb.py` ← `generators/duckdb.py`) then count, and the cost is only a little over-selection.

## (d) Ranked recommendations

1. **c3 + c4 together with c1 (import graph plus file closure). Fixes R1, R3, R4 and R5.** A Validator helper edit becomes about 1000 dialect tests (a few seconds of load), not a sure 300 s miss. A syntax error or module-level edit in test_dialect.py or helpers.py reaches every importer. This is the biggest recall gain in this area, and it costs nothing on patches that touch only sqlglot.
2. **The class-attribute and hook rule (`scopes`). Fixes R2 and R6.** Edits to `dialect`, `maxDiff`, `setUp`, `setUpClass` or any TestCase-name override select the class plus every subclass in other files. For test_executor/optimizer/lineage this narrows the result from the whole file to the class, which is the same set today because each has one class.
3. **The call closure for helpers (`class_closure`, `name_closure`).** This narrows W1 and W2 without hurting recall:
   - `assert_logger_contains` → parser, optimizer and transpile tests that call it;
   - `assert_duckdb_sql` → 4 call sites in 2 files;
   - `validate_transpile` → 15 uses in 3 files;
   - `rename_anonymous` → its callers only, so execute_tpcds is no longer forced in.

   Identifier strings count as reads, so `getattr(self, "x")` is covered. If a changed helper has no static caller, its whole class tree is selected.
4. **Selection by AST unit diff (`test_units`), not by touched lines. Fixes W4.** Line moves, inserted blank lines and new test methods select nothing beyond the changed units. A removed or renamed test keeps its old id.
5. **c5 (R7).** This is cheap insurance against a sqlglot rename that breaks `tests/dialects/test_dialect.py:16/19` imports and with it all 34 dialect files.
6. W3/W5: return an empty set for added test files and for `__init__.py` files of packages with no tests in the run, such as tests/sqlglot/. Today they select everything.

Known gaps in this design. Each can only over-select or fall back.
- Dynamic name lookups (`globals()[...]`) are not seen.
- Every non-literal module-level statement in helpers.py (for example `FIXTURES_DIR = os.path.join(...)`) is treated as able to raise at import. That reaches every importer, which is nearly all tests. It is safe, but on purpose it does not narrow.
