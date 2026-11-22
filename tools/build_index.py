"""Static index of sqlglot at the pinned commit, built during `docker build` (not charged).

    python tools/build_index.py <sqlglot checkout> <out.json>

It holds what coverage cannot see for code that runs at import time:
  toplevel  file -> [[start, end, name]] of top-level classes
  lambdas   file -> [[start, end]] of lambdas outside any function (class tables)
  refs      class name -> [[file, start, end]] lines that name the class, plus the
            bodies of generator methods named after it (`<lower>_sql`)
  subs      class name -> names of direct subclasses
  rdeps     module file -> module files that import it
  fns       function name -> [[file, start, end]] of every definition with that name
Never fails the build: on any error it writes an empty index and the selector falls back.
"""

import ast
import io
import json
import re
import sys
import tokenize
from pathlib import Path


CAMEL = re.compile(r"(?<!^)(?=[A-Z])")  # same as sqlglot.helper.CAMEL_CASE_PATTERN
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
CALL = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def sql_names_of(cls: ast.ClassDef) -> set[str]:
    """Lower-case SQL names a Func class answers to: the default snake case name, the class name,
    and any _sql_names entries."""
    out = {CAMEL.sub("_", cls.name).lower(), cls.name.lower()}
    for st in cls.body:
        targets = st.targets if isinstance(st, ast.Assign) else [st.target] if isinstance(st, ast.AnnAssign) else []
        if any(getattr(t, "id", None) == "_sql_names" for t in targets) and st.value is not None:
            out |= {e.value.lower() for e in ast.walk(st.value) if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return out


def names_fixture(strings, name: str, stem: str) -> bool:
    """A string names the fixture when its last path component is the file's name or stem:
    "simplify", "optimizer/qualify_columns__invalid.sql", "tpc-h/tpc-h". A directory string such as
    "optimizer/" or "/optimizer/tpc-ds/" does not (same rule as selector.py)."""
    return any(s.rsplit("/", 1)[-1] in (name, stem) for s in strings)


def scan_call_tests(root: Path, wanted: set[str]) -> dict[str, list]:
    """SQL function name -> test ids whose strings (any length) or fixtures call it.
    "*" marks a fixture calling it that no test can be tied to."""
    calls: dict[str, set] = {}
    strings_of: dict[str, set] = {}
    for path in sorted((root / "tests").rglob("*.py")):
        if "fixtures" in path.parts:
            continue
        rel = str(path.relative_to(root))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue

        def strs_in(nodes) -> set[str]:
            return {n.value for node in nodes for n in ast.walk(node)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)}

        units = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test"):
                units.append((f"{rel}::{node.name}", [node]))
            elif isinstance(node, ast.ClassDef):
                helpers = [i for i in node.body if isinstance(i, ast.FunctionDef) and not i.name.startswith("test")]
                units += [(f"{rel}::{node.name}::{i.name}", [i] + helpers) for i in node.body
                          if isinstance(i, ast.FunctionDef) and i.name.startswith("test")]
        for tid, nodes in units:
            strs = strs_in(nodes)
            strings_of[tid] = strs
            words = {w.lower() for s in strs for w in CALL.findall(s)} | {s.lower() for s in strs if IDENT.fullmatch(s)}
            for w in words & wanted:
                calls.setdefault(w, set()).add(tid)
    fixtures = root / "tests" / "fixtures"
    if fixtures.is_dir():
        for p in sorted(fixtures.rglob("*")):
            if not p.is_file() or p.suffix not in (".sql", ".json", ".csv", ".txt"):
                continue
            try:
                words = {w.lower() for w in CALL.findall(p.read_text(encoding="utf-8", errors="ignore"))} & wanted
            except Exception:
                continue
            if not words:
                continue
            users = ({t for t, ss in strings_of.items() if names_fixture(ss, p.name, p.stem)}
                     or {t for t, ss in strings_of.items() if any(p.stem in s for s in ss)}
                     or {t for t, ss in strings_of.items() if any(p.parent.name in s for s in ss)} or {"*"})
            for w in words:
                calls.setdefault(w, set()).update(users)
    return {k: sorted(v) for k, v in calls.items()}


def module_file(name: str, root: Path) -> str | None:
    parts = name.split(".")
    for cand in (Path(*parts).with_suffix(".py"), Path(*parts) / "__init__.py"):
        if (root / cand).is_file():
            return str(cand)
    return None


def build(root: Path) -> dict:
    files = sorted(p for p in (root / "sqlglot").rglob("*.py") if "__pycache__" not in p.parts)
    trees: dict[str, ast.AST] = {}
    texts: dict[str, str] = {}
    for p in files:
        rel = str(p.relative_to(root))
        try:
            texts[rel] = p.read_text(encoding="utf-8")
            trees[rel] = ast.parse(texts[rel])
        except Exception:
            continue

    toplevel: dict[str, list] = {}
    lambdas: dict[str, list] = {}
    subs: dict[str, set] = {}
    class_names: set[str] = set()
    sql_methods: dict[str, list] = {}  # "<lower>_sql" -> ranges
    fn_ranges: dict[str, list] = {}  # function name -> every definition's range
    rdeps: dict[str, set] = {}
    internal_names: dict[str, set] = {}  # "sqlglot/x.py:name" -> sqlglot modules that import name from x

    for rel, tree in trees.items():
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                toplevel.setdefault(rel, []).append([start, node.end_lineno, node.name])
                class_names.add(node.name)
                for base in node.bases:
                    bname = base.attr if isinstance(base, ast.Attribute) else base.id if isinstance(base, ast.Name) else None
                    if bname:
                        subs.setdefault(bname, set()).add(node.name)

        # Lambdas that are not inside a function: walk with a function-depth flag.
        def visit(node, in_fn: bool) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fn_ranges.setdefault(child.name, []).append([rel, child.lineno, child.end_lineno])
                    if child.name.endswith("_sql"):
                        sql_methods.setdefault(child.name, []).append([rel, child.lineno, child.end_lineno])
                    visit(child, True)
                elif isinstance(child, ast.Lambda):
                    if not in_fn:
                        lambdas.setdefault(rel, []).append([child.lineno, child.end_lineno])
                    visit(child, True)
                else:
                    visit(child, in_fn)

        visit(tree, False)

        pkg = rel[:-3].replace("/", ".")
        if pkg.endswith(".__init__"):
            pkg = pkg[: -len(".__init__")]
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    anchor = pkg.split(".")
                    if not rel.endswith("__init__.py"):
                        anchor = anchor[:-1]
                    anchor = anchor[: len(anchor) - (node.level - 1)]
                    base = ".".join(anchor + ([base] if base else []))
                targets = [base] + [f"{base}.{a.name}" for a in node.names]
                if base.startswith("sqlglot"):
                    src = module_file(base, root)
                    for a in node.names:  # (defining module, name) -> sqlglot modules importing it
                        if src:
                            internal_names.setdefault(f"{src}:{a.name}", set()).add(rel)
            for t in targets:
                f = module_file(t, root)
                if f and f != rel:
                    rdeps.setdefault(f, set()).add(rel)

    # Factories another module calls at its own import time (a dialect's class body calling a helper
    # from dialect.py or transforms.py): their own lines run at import, so seed them in their file.
    top_calls: dict[str, set] = {}
    for rel, tree in trees.items():
        names, todo = set(), list(ast.iter_child_nodes(tree))
        while todo:
            n = todo.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(n, ast.Call):
                fnode = n.func
                nm = fnode.id if isinstance(fnode, ast.Name) else fnode.attr if isinstance(fnode, ast.Attribute) else None
                if nm:
                    names.add(nm)
            todo.extend(ast.iter_child_nodes(n))
        top_calls[rel] = names
    # Only dialect modules' import-time calls (class-body tables built with dialect.py/transforms.py
    # factories): seeding every module-level call would mark busy runtime helpers (builders, helper)
    # as import-time and widen their edits to whole-file coverage.
    dialect_dirs = ("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/", "sqlglot/tokenizers/", "sqlglot/typing/")
    xseeds: dict[str, set] = {}
    for key, importers in internal_names.items():
        src, name = key.rsplit(":", 1)
        if any(imp.startswith(dialect_dirs) and name in top_calls.get(imp, ()) for imp in importers):
            xseeds.setdefault(src, set()).add(name)

    # Functions that run at import / class creation: hooks (__init_subclass__, __set_name__,
    # a metaclass __new__), anything called outside a def or lambda, and their same-file callees.
    # Coverage credits their lines only to whichever test imported first.
    import_fns: dict[str, list] = {}
    for rel, tree in trees.items():
        module_names = set()  # names bound at module level (caches live here)
        for st in tree.body:
            for tg in (st.targets if isinstance(st, ast.Assign) else [st.target] if isinstance(st, ast.AnnAssign) else []):
                if isinstance(tg, ast.Name):
                    module_names.add(tg.id)
        defs: dict[str, list] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defs.setdefault(node.name, []).append(node)

        def callee(c: ast.Call) -> str | None:
            f = c.func
            return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None

        def own_calls(fn) -> set[str]:
            """Calls made by fn's own body, not by defs or lambdas nested in it (those run later)."""
            out, todo = set(), list(ast.iter_child_nodes(fn))
            while todo:
                n = todo.pop()
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                    continue
                if isinstance(n, ast.Call) and callee(n):
                    out.add(callee(n))
                todo.extend(ast.iter_child_nodes(n))
            return out

        seeds: set[str] = set()

        def scan(node) -> None:
            for ch in ast.iter_child_nodes(node):
                if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if ch.name in ("__init_subclass__", "__set_name__"):
                        seeds.add(ch.name)
                    for d in ch.decorator_list:  # decorators run at definition time
                        for c in ast.walk(d):
                            if isinstance(c, ast.Call) and callee(c):
                                seeds.add(callee(c))
                    continue
                if isinstance(ch, ast.Lambda):
                    continue
                if isinstance(ch, ast.ClassDef) and any(
                    (b.id if isinstance(b, ast.Name) else b.attr if isinstance(b, ast.Attribute) else "") in ("type", "ABCMeta")
                    for b in ch.bases
                ):
                    seeds.add("__new__")
                    seeds.add("__init__")
                    seeds.add("__call__")
                if isinstance(ch, ast.Call) and callee(ch):
                    seeds.add(callee(ch))
                scan(ch)

        scan(tree)
        seeds |= xseeds.get(rel, set())
        # Run-once functions: caches and lazily built dispatch tables run on first use in each
        # worker process, so coverage credits them to whichever test came first. A function whose
        # result its caller stores in a module-level cache (_DISPATCH_CACHE[cls] = _build_dispatch(cls))
        # is one of them; a name prefix is not evidence (most _build*/_builder run on every parse).
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            made_by: dict[str, set] = {}  # local name -> functions whose calls produced it
            for n in ast.walk(fn):
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and callee(n.value):
                    for tg in n.targets:
                        if isinstance(tg, ast.Name):
                            made_by.setdefault(tg.id, set()).add(callee(n.value))
            for n in ast.walk(fn):
                if not isinstance(n, ast.Assign):
                    continue
                cached = any(isinstance(tg, ast.Subscript) and isinstance(tg.value, ast.Name)
                             and tg.value.id in module_names for tg in n.targets)
                if not cached:
                    continue
                if isinstance(n.value, ast.Call) and callee(n.value):
                    seeds.add(callee(n.value))
                elif isinstance(n.value, ast.Name) and n.value.id in made_by:
                    seeds |= made_by[n.value.id]
        for name, nodes in defs.items():
            for d in nodes:
                deco = {(x.func if isinstance(x, ast.Call) else x) for x in d.decorator_list}
                deco_names = {x.attr if isinstance(x, ast.Attribute) else getattr(x, "id", "") for x in deco}
                once = bool(deco_names & {"cache", "lru_cache", "cached_property", "cache_info"})
                for n in ast.walk(d):
                    if isinstance(n, (ast.Global, ast.Nonlocal)):
                        once = True
                    elif isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                        targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                        for t in targets:
                            base = t.value if isinstance(t, (ast.Attribute, ast.Subscript)) else None
                            if isinstance(base, ast.Name) and base.id == "cls":
                                once = True
                if once:
                    seeds.add(name)
        hot, todo = set(), list(seeds)
        while todo:
            n = todo.pop()
            if n in hot or n not in defs:
                continue
            hot.add(n)
            for d in defs[n]:
                todo.extend(own_calls(d))
        entries = []
        for n in sorted(hot):
            for d in defs[n]:
                nested = [[c.lineno, c.end_lineno] for c in ast.walk(d)
                          if c is not d and isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]
                entries.append([d.lineno, d.end_lineno, nested])
        if entries:
            import_fns[rel] = entries

    # Dialects named by fixture meta headers ("# dialect: snowflake"): tests reading those
    # fixtures exercise the dialect without naming it in Python.
    fixture_dialects: dict[str, list] = {}
    fixtures = root / "tests" / "fixtures"
    if fixtures.is_dir():
        for p in sorted(fixtures.rglob("*.sql")):
            try:
                names = set()
                for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("#") and ":" in line:
                        key, val = line[1:].split(":", 1)
                        if key.strip() in ("dialect", "read", "write"):
                            names.update(v.strip() for v in val.split(",") if v.strip())
                for name in names:
                    fixture_dialects.setdefault(name, []).append(str(p.relative_to(root)))
            except Exception:
                continue

    # Dialects used by name inside sqlglot (dialect="spark", Dialect["x"], get_or_raise("x")):
    # the user depends on that dialect's module even without importing it.
    dialect_stems = {p.stem for p in (root / "sqlglot/dialects").glob("*.py")} - {"__init__", "dialect"}
    for rel, tree in trees.items():
        try:
            for n in ast.walk(tree):
                lits = []
                if isinstance(n, ast.Call):
                    lits += [k.value for k in n.keywords if k.arg in ("dialect", "read", "write")]
                    if getattr(n.func, "attr", "") in ("get_or_raise", "get"):
                        lits += n.args[:1]
                elif isinstance(n, ast.Subscript) and getattr(n.value, "id", "") == "Dialect":
                    lits.append(n.slice)
                for c in lits:
                    if isinstance(c, ast.Constant) and isinstance(c.value, str) and c.value in dialect_stems:
                        target = f"sqlglot/dialects/{c.value}.py"
                        if target != rel:
                            rdeps.setdefault(target, set()).add(rel)
        except Exception:
            continue

    # Tests that loop over every dialect (the Dialects enum or the registry) without naming one:
    # any dialect's import-time edit can break them.
    all_dialect_tests: list[str] = []
    for p in sorted((root / "tests").rglob("test_*.py")):
        rel = str(p.relative_to(root))
        try:
            ttree = ast.parse(p.read_text(encoding="utf-8"))
        except Exception:
            continue

        def loops_all(fn) -> bool:
            for n in ast.walk(fn):
                if isinstance(n, ast.Name) and n.id == "Dialects":
                    return True
                if isinstance(n, ast.Attribute) and n.attr in ("classes", "_classes") \
                        and getattr(n.value, "id", "") == "Dialect":
                    return True
            return False

        for node in ttree.body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test") and loops_all(node):
                all_dialect_tests.append(f"{rel}::{node.name}")
            elif isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name.startswith("test") and loops_all(item):
                        all_dialect_tests.append(f"{rel}::{node.name}::{item.name}")

    # Imports between test modules (tests.helpers, tests.dialects.test_dialect.Validator...):
    # an edit outside a test method reaches every test file that imports the edited one.
    test_rdeps: dict[str, set] = {}
    sqlglot_names: dict[str, set] = {}  # name imported from sqlglot -> test modules importing it
    for p in sorted((root / "tests").rglob("*.py")):
        rel = str(p.relative_to(root))
        if "fixtures" in p.parts:
            continue
        try:
            ttree = ast.parse(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        pkg_parts = rel[:-3].split("/")
        for node in ast.walk(ttree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    anchor = pkg_parts[:-1] if not rel.endswith("__init__.py") else pkg_parts[:-1]
                    anchor = anchor[: len(anchor) - (node.level - 1)]
                    base = ".".join(anchor + ([base] if base else []))
                names = [base] + [f"{base}.{a.name}" for a in node.names]
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("sqlglot"):
                for a in node.names:  # names a test module imports from sqlglot
                    sqlglot_names.setdefault(a.name, set()).add(rel)
            for name in names:
                if not name.startswith("tests"):
                    continue
                target = module_file(name, root)
                if target and target != rel:
                    test_rdeps.setdefault(target, set()).add(rel)

    # Funcs are registered by SQL name (FUNCTION_BY_NAME, dialect FUNCTIONS rows) and printed by
    # the generic function_fallback_sql, so often no executed line names the class. Map each
    # class to the SQL names that build or print it, and each name to the tests calling it.
    calls: dict[str, set] = {}
    for rel, tree in trees.items():
        try:
            for node in ast.walk(tree):
                if not isinstance(node, ast.Dict):
                    continue
                for k, v in zip(node.keys, node.values):
                    if k is None:
                        continue
                    kname = k.attr if isinstance(k, ast.Attribute) else k.id if isinstance(k, ast.Name) else None
                    if kname in class_names:  # exp.Foo: rename_func("NAME") / "NAME"
                        for sub in ast.walk(v):
                            if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and IDENT.fullmatch(sub.value):
                                calls.setdefault(kname, set()).add(sub.value.lower())
                    elif isinstance(k, ast.Constant) and isinstance(k.value, str) and IDENT.fullmatch(k.value):
                        for sub in ast.walk(v):  # "NAME": exp.Foo.from_arg_list / lambda: exp.Foo(...)
                            n = sub.attr if isinstance(sub, ast.Attribute) else sub.id if isinstance(sub, ast.Name) else None
                            if n in class_names:
                                calls.setdefault(n, set()).add(k.value.lower())
            if rel.startswith("sqlglot/expressions/"):
                for node in tree.body:
                    if isinstance(node, ast.ClassDef):
                        calls.setdefault(node.name, set()).update(sql_names_of(node))
        except Exception:
            continue
    wanted = set().union(*calls.values()) if calls else set()
    try:
        call_tests = scan_call_tests(root, wanted)
    except Exception as e:
        print(f"scan_call_tests failed: {e}", file=sys.stderr)
        call_tests = None

    refs: dict[str, list] = {}
    for rel, text in texts.items():
        try:
            toks = tokenize.generate_tokens(io.StringIO(text).readline)
            seen = set()
            prev = ["", ""]  # the two previous significant token strings
            for tok in toks:
                if tok.type in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT):
                    continue
                typing_attr = prev[1] == "." and prev[0] in ("t", "typing")  # t.Any is not exp.Any
                prev = [prev[1], tok.string]
                if typing_attr:
                    continue
                if tok.type == tokenize.NAME and tok.string in class_names:
                    key = (tok.string, tok.start[0])
                    if key not in seen:
                        seen.add(key)
                        refs.setdefault(tok.string, []).append([rel, tok.start[0], tok.start[0]])
        except Exception:
            continue
    for name in class_names:
        refs.setdefault(name, []).extend(sql_methods.get(f"{name.lower()}_sql", []))

    # "Cls.attr" -> files reading it that way: deleting that class attribute breaks those files.
    qualified_attrs: dict[str, set] = {}
    for rel, tree in trees.items():
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute):
                v = n.value
                owner = v.id if isinstance(v, ast.Name) else v.attr if isinstance(v, ast.Attribute) else None
                if owner in class_names:
                    qualified_attrs.setdefault(f"{owner}.{n.attr}", set()).add(rel)

    extra: dict = {"qualified_attrs": {k: sorted(v) for k, v in qualified_attrs.items()}}
    for part in (eq_tables, construct_tables, attr_read_tables):
        try:
            extra.update(part(root, trees, all_dialect_tests))
        except Exception as e:  # optional tables: the selector refuses the rules that need them
            print(f"{part.__name__} failed: {type(e).__name__}: {e}", file=sys.stderr)

    return {
        **extra,
        "toplevel": toplevel,
        "lambdas": lambdas,
        "refs": refs,
        "subs": {k: sorted(v) for k, v in subs.items()},
        "rdeps": {k: sorted(v) for k, v in rdeps.items()},
        "fns": fn_ranges,
        "import_fns": import_fns,
        "all_dialect_tests": all_dialect_tests,
        "test_rdeps": {k: sorted(v) for k, v in test_rdeps.items()},
        "sqlglot_names": {k: sorted(v) for k, v in sqlglot_names.items()},
        "internal_names": {k: sorted(v) for k, v in internal_names.items()},
        **({"calls": {k: sorted(v) for k, v in calls.items()}, "call_tests": call_tests}
           if call_tests is not None else {}),
        "fixture_dialects": fixture_dialects,
    }


def _parse_tests(root: Path) -> dict[str, ast.AST]:
    out = {}
    for p in sorted((root / "tests").rglob("*.py")):
        if "fixtures" in p.parts:
            continue
        try:
            out[str(p.relative_to(root))] = ast.parse(p.read_text(encoding="utf-8"))
        except Exception:
            continue
    return out


ENUM_BASES = {"Enum", "AutoName", "IntEnum", "StrEnum", "Flag", "IntFlag"}
MIXIN_BASES = {"str", "int", "IntEnum", "StrEnum", "IntFlag", "float", "bytes"}


def eq_tables(root: Path, trees: dict, _all_dialect_tests) -> dict:
    """Facts for proving an `is` <-> `==` swap equivalent (selector.equivalent_swap):
      eq_unsafe    class -> True when its __eq__/__ne__ reads `other` without a type guard
      class_bases  class -> base names (sqlglot and tests); class_subs the reverse
      enum_kind    enum class -> "pure" | "mixin" (str/int mixins compare equal to raw values)
      attr_ann     "file:attr" -> annotation of `self.attr: T` (any method of that file)
      attr_enum    attr -> enum name when every value ever stored under that attribute name
                   (sqlglot and tests: `x.attr = v`, class-body `attr = v`, `attr=` keywords) is a
                   member of that one enum, a call of it, None, or self.OTHER that passes too
    """
    files = dict(trees)
    files.update(_parse_tests(root))
    eq_unsafe: dict[str, bool] = {}
    bases_of: dict[str, set] = {}
    enums: dict[str, str] = {}
    ann: dict[str, str] = {}
    assigns: dict[str, list] = {}
    dynamic: set[str] = set()  # attribute names set through setattr / __dict__ with a literal name

    def unsafe(fn) -> bool:
        if len(fn.args.args) < 2:
            return True
        other = fn.args.args[1].arg
        src = ast.unparse(fn)
        guarded = "isinstance(" in src or "type(" in src
        touches = any(isinstance(n, (ast.Attribute, ast.Subscript)) and isinstance(n.value, ast.Name)
                      and n.value.id == other for n in ast.walk(fn))
        return touches and not guarded

    pending = []
    for rel, tree in files.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bnames = {ast.unparse(b.value if isinstance(b, ast.Subscript) else b).split(".")[-1]
                          for b in node.bases}
                bases_of.setdefault(node.name, set()).update(bnames)
                pending.append((node.name, bnames))
                for it in node.body:
                    if isinstance(it, (ast.FunctionDef, ast.AsyncFunctionDef)) and it.name in ("__eq__", "__ne__"):
                        eq_unsafe[node.name] = eq_unsafe.get(node.name, False) or unsafe(it)
                    elif isinstance(it, (ast.Assign, ast.AnnAssign)) and it.value is not None:
                        for tg in (it.targets if isinstance(it, ast.Assign) else [it.target]):
                            if isinstance(tg, ast.Name):
                                assigns.setdefault(tg.id, []).append(it.value)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                for tg in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                    if isinstance(tg, ast.Attribute):
                        assigns.setdefault(tg.attr, []).append(
                            node.value if node.value is not None and not isinstance(node, ast.AugAssign) else None)
                        if isinstance(node, ast.AnnAssign) and isinstance(tg.value, ast.Name) and tg.value.id == "self":
                            ann[f"{rel}:{tg.attr}"] = ast.unparse(node.annotation)
                    elif isinstance(tg, ast.Subscript) and isinstance(tg.value, ast.Attribute) and tg.value.attr == "__dict__":
                        dynamic.add("*")
            elif isinstance(node, ast.keyword) and node.arg:
                assigns.setdefault(node.arg, []).append(node.value)
            elif isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("setattr", "delattr"):
                if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                    dynamic.add(node.args[1].value)
                else:
                    dynamic.add("*")  # computed attribute names: no provenance proof anywhere

    changed = True
    while changed:  # enums subclassing enums
        changed = False
        for name, bnames in pending:
            if name in enums:
                continue
            if bnames & ENUM_BASES or any(b in enums for b in bnames):
                mixin = bool(bnames & MIXIN_BASES) or any(enums.get(b) == "mixin" for b in bnames)
                enums[name] = "mixin" if mixin else "pure"
                changed = True

    def member_of(v, seen) -> str | None:
        """The enum v is a member of ("" for None), or None when unknown."""
        if v is None:
            return None
        if isinstance(v, ast.Constant) and v.value is None:
            return ""
        if isinstance(v, ast.Call):
            head = ast.unparse(v.func).split(".")[-1]
            return head if head in enums else None
        if isinstance(v, ast.Attribute):
            if isinstance(v.value, ast.Name) and v.value.id in ("self", "cls"):
                return attr_enum(v.attr, seen)
            head = ast.unparse(v.value).split(".")[-1]
            return head if head in enums else None
        return None

    memo: dict[str, str | None] = {}

    def attr_enum(attr, seen) -> str | None:
        if attr in memo:
            return memo[attr]
        if attr in seen:
            return ""  # a cycle adds no new values
        if "*" in dynamic or attr in dynamic or not assigns.get(attr):
            return None
        kinds = set()
        for v in assigns[attr]:
            k = member_of(v, seen | {attr})
            if k is None:
                if not seen:
                    memo[attr] = None
                return None
            kinds.add(k)
        kinds.discard("")
        res = kinds.pop() if len(kinds) == 1 else None
        if not seen:
            memo[attr] = res
        return res

    attr_enum_map = {}
    for a in assigns:
        if a.isidentifier():
            e = attr_enum(a, frozenset())
            if e:
                attr_enum_map[a] = e
    subs_all: dict[str, set] = {}
    for c, bs in bases_of.items():
        for b in bs:
            subs_all.setdefault(b, set()).add(c)
    return {
        "eq_unsafe": eq_unsafe,
        "class_bases": {k: sorted(v) for k, v in bases_of.items()},
        "class_subs": {k: sorted(v) for k, v in subs_all.items()},
        "enum_kind": enums,
        "attr_ann": ann,
        "attr_enum": attr_enum_map,
    }


def construct_tables(root: Path, trees: dict, all_dialect_tests) -> dict:
    """Facts for skipping construct-only tests on edits that cannot fail at import:
      closure_factories  names whose every definition only defines and returns a closure
      gen_init_attrs     attribute names read while building a generator (Generator.__init__,
                         Dialect.generator): a value there can change construction behaviour
      construct_only_tests  all-dialect tests that only build objects and measure memory
                         (tracemalloc), never parsing or generating SQL from the edited tables
    """
    factory: dict[str, bool] = {}
    for tree in trees.values():
        for n in ast.walk(tree):
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = list(n.body)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                body = body[1:]  # docstring
            ok = len(body) >= 2 and isinstance(body[-1], ast.Return) and body[-1].value is not None
            if ok:
                defined = set()
                for st in body[:-1]:
                    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        defined.add(st.name)
                    elif isinstance(st, ast.Assign) and isinstance(st.value, ast.Lambda) and \
                            all(isinstance(tg, ast.Name) for tg in st.targets):
                        defined.update(tg.id for tg in st.targets)
                    else:
                        ok = False
                        break
                ret = body[-1].value
                ok = ok and (isinstance(ret, ast.Lambda) or (isinstance(ret, ast.Name) and ret.id in defined))
            factory[n.name] = factory.get(n.name, True) and ok
    gen_attrs: set[str] = set()
    for rel, cls_name, fn_name in (("sqlglot/generator.py", "Generator", "__init__"),
                                   ("sqlglot/dialects/dialect.py", "Dialect", "generator")):
        tree = trees.get(rel)
        if tree is None:
            raise ValueError(f"{rel} missing")
        found = False
        for c in tree.body:
            if isinstance(c, ast.ClassDef) and c.name == cls_name:
                for it in c.body:
                    if isinstance(it, ast.FunctionDef) and it.name == fn_name:
                        found = True
                        gen_attrs |= {a.attr for a in ast.walk(it) if isinstance(a, ast.Attribute)}
                        gen_attrs |= {k.arg for k in ast.walk(it) if isinstance(k, ast.keyword) and k.arg}
        if not found:
            raise ValueError(f"{cls_name}.{fn_name} missing")
    tests = _parse_tests(root)
    construct_only = []
    sql_calls = {"sql", "transpile", "parse", "parse_one", "validate_identity", "validate_all",
                 "generate", "tokenize", "optimize", "execute"}
    for tid in all_dialect_tests:
        rel, *rest = tid.split("::")
        tree = tests.get(rel)
        if tree is None:
            continue
        node = tree
        for part in rest:
            node = next((c for c in getattr(node, "body", []) if isinstance(c, (ast.ClassDef, ast.FunctionDef))
                         and c.name == part), None)
            if node is None:
                break
        if node is None:
            continue
        names = {n.attr if isinstance(n, ast.Attribute) else n.id for n in ast.walk(node)
                 if isinstance(n, (ast.Attribute, ast.Name))}
        if "tracemalloc" in names and not names & sql_calls:
            construct_only.append(tid)
    return {
        "closure_factories": sorted(k for k, v in factory.items() if v),
        "gen_init_attrs": sorted(gen_attrs),
        "construct_only_tests": construct_only,
    }


CONST_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def attr_read_tables(root: Path, trees: dict, _all_dialect_tests) -> dict:
    """Where class constants are read, for the value-reader rule (selector.value_readers):
      attr_reads  NAME -> [[file, line, where, owner, keyonly]] for every load of a class constant
                  (UPPER_CASE name assigned in some class body):
                  where   "fn" inside a def or lambda body (runs when called), "top" otherwise
                          (module level or class body: runs at import), "dyn" for a string
                          equal to NAME (getattr-style access)
                  owner   the name the attribute is read from ("self", "dialect", "MySQLParser"),
                          or "class:<C>" for a bare NAME read inside class C's body
                  keyonly 1 when only the keys can matter: iterated, tested with `in`, .keys(), len()
      test_attr_users  NAME -> test ids whose source reads .NAME or holds the string "NAME"
    """
    consts: set[str] = set()
    for tree in trees.values():
        for c in ast.walk(tree):
            if isinstance(c, ast.ClassDef):
                for s in c.body:
                    for tg in (s.targets if isinstance(s, ast.Assign) else [s.target] if isinstance(s, ast.AnnAssign) else []):
                        if isinstance(tg, ast.Name) and CONST_NAME.match(tg.id):
                            consts.add(tg.id)
    reads: dict[str, list] = {}
    hops: set[str] = set()

    def keyonly(node, parents) -> int:
        if direct_keyonly(node, parents):
            return 1
        # numeric_escapes = klass.tokenizer_class.NUMERIC_ESCAPES; ... "x" in numeric_escapes:
        # a local alias whose every use (in the same function) only looks at keys.
        p = parents.get(id(node))
        if isinstance(p, ast.Assign) and p.value is node and len(p.targets) == 1 and isinstance(p.targets[0], ast.Name):
            name, fn = p.targets[0].id, p
            while fn is not None and not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn = parents.get(id(fn))
            if fn is None:
                return 0
            uses = [n for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id == name]
            loads = [n for n in uses if isinstance(n.ctx, ast.Load)]
            stores = [n for n in uses if not isinstance(n.ctx, ast.Load)]
            if len(stores) == 1 and loads and all(direct_keyonly(n, parents) for n in loads):
                return 1
        return 0

    def direct_keyonly(node, parents) -> int:
        p = parents.get(id(node))
        if isinstance(p, ast.comprehension) and p.iter is node:
            return 1
        if isinstance(p, ast.For) and p.iter is node:
            return 1
        if isinstance(p, ast.Compare) and node in p.comparators and \
                all(isinstance(op, (ast.In, ast.NotIn)) for op in p.ops):
            return 1
        if isinstance(p, ast.Attribute) and p.attr == "keys":
            return 1
        if isinstance(p, ast.Call) and getattr(p.func, "id", "") == "len" and node in p.args:
            return 1
        return 0

    for rel, tree in trees.items():
        parents = {}
        for p in ast.walk(tree):
            for ch in ast.iter_child_nodes(p):
                parents[id(ch)] = p

        def handle(ch, in_fn: bool, cls: str | None) -> None:
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # decorators and defaults run at definition time, the body when called
                for d in ch.decorator_list + ch.args.defaults + [x for x in ch.args.kw_defaults if x]:
                    handle(d, in_fn, cls)
                for st in ch.body:
                    handle(st, True, None)
            elif isinstance(ch, ast.Lambda):
                for d in ch.args.defaults:
                    handle(d, in_fn, cls)
                handle(ch.body, True, None)
            elif isinstance(ch, ast.ClassDef):
                for b in ch.bases + ch.decorator_list + [k.value for k in ch.keywords]:
                    handle(b, in_fn, cls)
                for st in ch.body:
                    handle(st, in_fn, None if in_fn else ch.name)
            else:
                record(ch, in_fn, cls)
                for sub in ast.iter_child_nodes(ch):
                    handle(sub, in_fn, cls)

        def visit(node, in_fn: bool, cls: str | None) -> None:
            for ch in ast.iter_child_nodes(node):
                handle(ch, in_fn, cls)

        def stmt_line(n) -> int:
            """Coverage records a statement on its first line: the read's enclosing statement."""
            cur = n
            while cur is not None and not isinstance(cur, ast.stmt):
                if isinstance(parents.get(id(cur)), ast.Lambda):
                    return cur.lineno  # a lambda body is recorded where it sits, when called
                cur = parents.get(id(cur))
            return cur.lineno if cur is not None else n.lineno

        def record(n, in_fn, cls) -> None:
            if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load) and n.attr in consts:
                v = n.value
                owner = v.id if isinstance(v, ast.Name) else v.attr if isinstance(v, ast.Attribute) else ""
                entry = [rel, stmt_line(n), "fn" if in_fn else "top", owner, keyonly(n, parents)]
                kw = parents.get(id(n))
                if in_fn and isinstance(kw, ast.keyword) and kw.arg:
                    entry.append(kw.arg)  # passed on as kw.arg=...: its real readers are downstream
                    hops.add(kw.arg)
                reads.setdefault(n.attr, []).append(entry)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in consts and cls and not in_fn:
                reads.setdefault(n.id, []).append([rel, stmt_line(n), "top", f"class:{cls}", keyonly(n, parents)])
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in consts:
                reads.setdefault(n.value, []).append([rel, n.lineno, "dyn", "", 0])

        visit(tree, False, None)

    # Downstream readers of a keyword argument K (one hop): loads of `.K` (stored as self.K by the
    # callee), loads of a parameter named K, and "dyn" for the string "K" (kwargs["K"], getattr).
    hop_reads: dict[str, list] = {}
    for rel, tree in trees.items():
        parents = {}
        for p in ast.walk(tree):
            for ch in ast.iter_child_nodes(p):
                parents[id(ch)] = p
        def start(n) -> int:
            cur = n
            while cur is not None and not isinstance(cur, ast.stmt):
                if isinstance(parents.get(id(cur)), ast.Lambda):
                    return cur.lineno
                cur = parents.get(id(cur))
            return cur.lineno if cur is not None else n.lineno

        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                params = {a.arg for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs} & hops
                for n in ast.walk(fn):
                    if isinstance(n, ast.Name) and n.id in params and isinstance(n.ctx, ast.Load):
                        p = parents.get(id(n))
                        if isinstance(p, ast.Assign) and p.value is n and all(
                                isinstance(tg, ast.Attribute) and tg.attr == n.id for tg in p.targets):
                            continue  # self.K = K: a pass-through, read later as .K (recorded below)
                        hop_reads.setdefault(n.id, []).append([rel, start(n), "fn"])
        slot_strings = set()
        for n in ast.walk(tree):
            if isinstance(n, (ast.Assign, ast.AnnAssign)) and any(
                    isinstance(tg, ast.Name) and tg.id == "__slots__"
                    for tg in (n.targets if isinstance(n, ast.Assign) else [n.target])) and n.value is not None:
                slot_strings |= {id(c) for c in ast.walk(n.value)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and n.attr in hops and isinstance(n.ctx, ast.Load):
                cur = n
                while cur is not None and not isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    cur = parents.get(id(cur))
                p = parents.get(id(n))
                if cur is not None and not isinstance(cur, ast.Lambda) and isinstance(p, ast.Assign) and p.value is n \
                        and len(p.targets) == 1 and isinstance(p.targets[0], ast.Name):
                    # local = self.K: the value is used only where the local is loaded; any test using
                    # it ran one of those lines (each runs after the assignment, in the same call).
                    name = p.targets[0].id
                    uses = [x for x in ast.walk(cur) if isinstance(x, ast.Name) and x.id == name]
                    if sum(not isinstance(x.ctx, ast.Load) for x in uses) == 1:
                        for x in uses:
                            if isinstance(x.ctx, ast.Load):
                                hop_reads.setdefault(n.attr, []).append([rel, start(x), "fn"])
                        continue
                hop_reads.setdefault(n.attr, []).append([rel, start(n), "fn" if cur is not None else "top"])
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in hops and id(n) not in slot_strings:
                hop_reads.setdefault(n.value, []).append([rel, n.lineno, "dyn"])

    dialect_dirs = ("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/", "sqlglot/tokenizers/", "sqlglot/typing/")
    dialect_classes = {c.name for rel, tree in trees.items() if rel.startswith(dialect_dirs)
                       for c in tree.body if isinstance(c, ast.ClassDef)}
    users: dict[str, set] = {}
    idents: dict[str, set] = {}  # dialect-file class name -> tests naming it in code
    for rel, tree in _parse_tests(root).items():
        # A dialect class named in module-level code (imports excluded: an unused import runs nothing)
        # reaches every test of the module, e.g. DIALECT = TSQL() used inside the tests.
        mod_idents = {n.id if isinstance(n, ast.Name) else n.attr
                      for st in tree.body if not isinstance(st, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom))
                      for n in ast.walk(st) if isinstance(n, (ast.Name, ast.Attribute))} & dialect_classes
        for node in tree.body:
            units = []
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test"):
                units.append((f"{rel}::{node.name}", [node]))
            elif isinstance(node, ast.ClassDef):
                helpers = [i for i in node.body if isinstance(i, ast.FunctionDef) and not i.name.startswith("test")]
                units += [(f"{rel}::{node.name}::{i.name}", [i] + helpers) for i in node.body
                          if isinstance(i, ast.FunctionDef) and i.name.startswith("test")]
            for tid, nodes in units:
                for nd in nodes:
                    for n in ast.walk(nd):
                        name = n.attr if isinstance(n, ast.Attribute) else \
                            n.value if isinstance(n, ast.Constant) and isinstance(n.value, str) else None
                        if name in consts:
                            users.setdefault(name, set()).add(tid)
                        ident = n.id if isinstance(n, ast.Name) else n.attr if isinstance(n, ast.Attribute) else None
                        if ident in dialect_classes:
                            idents.setdefault(ident, set()).add(tid)
            for ident in mod_idents:
                idents.setdefault(ident, set()).update(t for t, _ in units)
    return {"attr_reads": reads, "hop_reads": hop_reads, "test_attr_users": {k: sorted(v) for k, v in users.items()},
            "test_idents": {k: sorted(v) for k, v in idents.items()}}


def main() -> None:
    root, out = Path(sys.argv[1]), Path(sys.argv[2])
    sys.setrecursionlimit(20000)  # deep ASTs in big generated tables
    try:
        index = build(root)
    except BaseException as e:  # never fail the image build (RecursionError included)
        print(f"build_index failed: {type(e).__name__}: {e}", file=sys.stderr)
        index = {}
    try:
        out.write_text(json.dumps(index, separators=(",", ":")))
    except Exception as e:
        print(f"build_index could not write {out}: {e}", file=sys.stderr)
    print(f"index: {', '.join(f'{k}={len(v)}' for k, v in index.items())}", file=sys.stderr)


if __name__ == "__main__":
    main()
