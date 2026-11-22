"""Runs the parts of tests that execute a commit's edited statements, on the patched sources.

    python stmt_probe.py SRC JOB.json  ->  stdout: one {"<test id>": true|false} line per test, as each finishes

JOB = {"overlay": {repo path: patched source}, "tests": [{"id": ..., "full": [stmt...],
       "cases": {stmt: {fixture file: [k...]}}}]}

Each test method is recompiled with only the top-level statements the selector needs (from the
statement map, tools/build_stmt_map.py), plus every statement a later one may depend on (anything
that binds a name used later, or calls something not on `self`). Fixture loaders yield only the
needed cases (numbered as in the full file) inside a statement that needs only cases. The result
is true when the reduced test passes: it ran every part of the test that reaches the edit, and
the selector only asks when the rest runs unchanged code on unchanged inputs.

duckdb and pandas load lazily (connect() replays its setup statements on first real use), so a
probe that never compares query results doesn't pay for importing them.
"""

import ast
import importlib.abc
import importlib.util
import json
import os
import sys
import types
import unittest
from concurrent.futures import Future
from pathlib import Path


class Overlay(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, root: Path, sources: dict):
        self.root, self.sources = root, sources

    def find_spec(self, name, path=None, target=None):
        rel = name.replace(".", "/")
        for cand, pkg in ((rel + ".py", False), (rel + "/__init__.py", True)):
            if cand in self.sources:
                origin = str(self.root / cand)
                spec = importlib.util.spec_from_loader(name, self, origin=origin, is_package=pkg)
                if pkg:
                    spec.submodule_search_locations = [str(self.root / rel)]
                spec.has_location = True
                return spec
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        rel = module.__spec__.origin[len(str(self.root)) + 1:]
        module.__file__ = module.__spec__.origin
        exec(compile(self.sources[rel], module.__file__, "exec"), module.__dict__)


# ---------------------------------------------------------------- lazy duckdb / pandas

class _LazyConn:
    def __init__(self, a, k):
        self._args, self._calls, self._real = (a, k), [], None

    def _get(self):
        if self._real is None:
            real = _load_real("duckdb")  # not import_module: that returns the lazy stub again
            self._real = real.connect(*self._args[0], **self._args[1])
            for name, a, k in self._calls:
                getattr(self._real, name)(*a, **k)
        return self._real

    def execute(self, *a, **k):
        r = _LazyResult(self, a, k)
        if self._real is None:
            self._calls.append(("execute", a, k))  # replayed when duckdb loads
        else:
            r._res = self._real.execute(*a, **k)  # runs now, read or not (DDL, inserts)
        return r

    def __getattr__(self, name):
        return getattr(self._get(), name)


class _LazyResult:
    """What conn.execute() returned: before duckdb loaded, it replays on first use; .df() gives a
    _Frame (rows, no pandas)."""

    def __init__(self, conn, a, k):
        self._conn, self._a, self._k, self._res = conn, a, k, None

    def _run(self):
        if self._res is None:
            conn = self._conn
            # Recorded before duckdb loaded. It is replayed with the others unless it was the
            # last call, which runs here instead, so its result is what the test reads.
            if conn._calls and conn._calls[-1][1] == self._a:
                conn._calls.pop()
            conn._get()
            self._res = conn._real.execute(*self._a, **self._k)
        return self._res

    def df(self):
        res = self._run()
        conn, a, k = self._conn, self._a, self._k
        return _Frame(res, lambda: conn._real.execute(*a, **k).df())

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._run(), name)


# DuckDB column types whose rows convert to a DataFrame without surprises: equal names, types and
# rows then mean equal frames. Anything else goes to pandas.
_PLAIN = ("BOOLEAN", "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "UTINYINT", "USMALLINT", "UINTEGER",
          "UBIGINT", "FLOAT", "DOUBLE", "VARCHAR", "DATE", "DECIMAL", '"NULL"', "NULL")


class _Frame:
    """What .df() returns in a probe: column names, DuckDB types and fetchall() rows. Becomes the
    real DataFrame (the query re-run, pandas imported) for any other use."""

    def __init__(self, res, real_df):
        self.names = tuple(d[0] for d in res.description)
        self.types = tuple(str(d[1]) for d in res.description)
        self.rows = res.fetchall()
        self._real_df, self._df = real_df, None

    def plain(self) -> bool:
        return all(t.split("(")[0] in _PLAIN for t in self.types)

    def real(self):
        if self._df is None:
            self._df = self._real_df()
        return self._df

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.real(), name)

    def __len__(self):
        return len(self.real())

    def __getitem__(self, key):
        return self.real()[key]

    def __iter__(self):
        return iter(self.real())


def _assert_frame_equal(*args, **kw):
    """pandas.testing.assert_frame_equal, decided without pandas when two probe frames are
    identical (names, types, rows) in plain types; pandas decides everything else."""
    if not kw and len(args) == 2 and all(isinstance(x, _Frame) for x in args):
        a, b = args
        if a.names == b.names and a.types == b.types and a.plain() and a.rows == b.rows:
            return None
    real = _load_real("pandas.testing")
    return real.assert_frame_equal(*[x.real() if isinstance(x, _Frame) else x for x in args], **kw)


def _load_real(name: str):
    """The real module behind a lazy stub: stop serving stubs, drop them, import for real."""
    import importlib
    if os.environ.get("PROBE_VERBOSE"):
        print(f"LOAD_REAL {name}", file=sys.stderr)
    for f in sys.meta_path:
        if isinstance(f, _LazyFinder):
            f.active = False
    root = name.split(".")[0]
    for n in list(sys.modules):
        if (n == root or n.startswith(root + ".")) and getattr(sys.modules[n], "__lazy__", False):
            del sys.modules[n]
    return importlib.import_module(name)


class _LazyFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    NAMES = {"duckdb", "pandas", "pandas.testing"}

    def __init__(self):
        self.active = True

    def find_spec(self, name, path=None, target=None):
        if self.active and name in self.NAMES:
            return importlib.util.spec_from_loader(name, self, is_package=name == "pandas")
        return None

    def create_module(self, spec):
        return types.ModuleType(spec.name)

    def exec_module(self, module):
        name = module.__name__

        def real():
            return _load_real(name)

        module.__lazy__ = True
        if name == "duckdb":
            module.connect = lambda *a, **k: _LazyConn(a, k)
        if name == "pandas":
            module.__path__ = []

        def __getattr__(attr):
            if attr.startswith("__"):
                raise AttributeError(attr)
            if name == "pandas.testing" and attr == "assert_frame_equal":
                return _assert_frame_equal
            return getattr(real(), attr)

        module.__getattr__ = __getattr__


# ---------------------------------------------------------------- reduced tests

class SyncExec:
    def submit(self, fn, *a, **k):
        f = Future()
        try:
            f.set_result(fn(*a, **k))
        except BaseException as e:  # noqa: BLE001
            f.set_exception(e)
        return f

    def shutdown(self, *a, **k):
        pass

    def map(self, fn, *its, **k):
        return [fn(*a) for a in zip(*its)]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def names(node, ctx) -> set:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ctx)}


def self_calls_only(stmt) -> bool:
    """Every simple statement inside is a call on `self` (or pass/docstring), so dropping it
    can't change what later statements see."""
    if isinstance(stmt, ast.Expr):
        v = stmt.value
        if isinstance(v, ast.Constant):
            return True
        return (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                and isinstance(v.func.value, ast.Name) and v.func.value.id == "self")
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, (ast.For, ast.With)):
        if isinstance(stmt, ast.With):
            for it in stmt.items:
                c = it.context_expr
                if not (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                        and isinstance(c.func.value, ast.Name) and c.func.value.id == "self"):
                    return False
        return all(self_calls_only(s) for s in stmt.body) and not getattr(stmt, "orelse", None)
    return False


def global_store(stmt, local: set) -> bool:
    """Writes through something that isn't a local name (module state, self, imported objects)."""
    for n in ast.walk(stmt):
        if isinstance(n, (ast.Attribute, ast.Subscript)) and isinstance(n.ctx, (ast.Store, ast.Del)):
            root = n.value
            while isinstance(root, (ast.Attribute, ast.Subscript, ast.Call)):
                root = root.value if not isinstance(root, ast.Call) else root.func
            if not (isinstance(root, ast.Name) and root.id in local):
                return True
        if isinstance(n, (ast.Global, ast.Nonlocal, ast.Return, ast.Yield, ast.YieldFrom,
                          ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef)):
            return True
    return False


def reduce_method(fdef, keep: set):
    """Keep the needed statements, every statement that may see their effects (reads a local name
    a kept statement wrote or used, transitively), and every statement those depend on (writes or
    may mutate a local they read, or writes non-local state). Drop the rest."""
    body = fdef.body
    local = names(fdef, ast.Store) | {a.arg for a in ast.walk(fdef.args) if isinstance(a, ast.arg)}
    local.discard("self")
    tainted: set = set()
    req = set()
    for i, s in enumerate(body):
        loads, stores = names(s, ast.Load), names(s, ast.Store)
        if i in keep or (loads & tainted):
            req.add(i)
            tainted |= (loads | stores) & local
    need: set = set()
    for i in range(len(body) - 1, -1, -1):
        s = body[i]
        loads, stores = names(s, ast.Load) & local, names(s, ast.Store) & local
        if i not in req:
            if stores & need or global_store(s, local):
                req.add(i)
            elif loads & need and not self_calls_only(s):
                req.add(i)  # may mutate an object a kept statement reads
        if i in req:
            need |= loads
    out = [s for i, s in enumerate(body) if i in req]
    if not out:
        out = [ast.Pass(lineno=fdef.lineno, col_offset=0)]
    fdef.body = out
    return fdef


CUR = {"spans": None, "code": None, "cases": {}}


def current_stmt():
    f = sys._getframe(2)
    while f is not None:
        if f.f_code is CUR["code"]:
            for j, (a, b) in CUR["spans"]:
                if a <= f.f_lineno <= b:
                    return j
            return None
        f = f.f_back
    return None


def wrap_loader(orig):
    def loader(filename):
        j = current_stmt()
        want = CUR["cases"].get(j)
        allowed = None if want is None else set(want.get(filename, ()))
        for k, item in enumerate(orig(filename), 1):
            if allowed is None or k in allowed:
                yield item
    return loader


_DEFS: dict = {}


def find_def(path: str, firstlineno: int):
    """A fresh copy of the def starting at that line (reduce_method edits it)."""
    import copy
    defs = _DEFS.get(path)
    if defs is None:
        defs = _DEFS[path] = {}
        for n in ast.walk(ast.parse(Path(path).read_text())):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defs.setdefault(min([n.lineno] + [d.lineno for d in n.decorator_list]), n)
                defs.setdefault(n.lineno, n)
    n = defs.get(firstlineno)
    return copy.deepcopy(n) if n is not None else None


def run_one(job, mods) -> bool:
    file, cname, mname = job["id"].split("::")
    mod = mods.get(file)
    if mod is None:
        mod = mods[file] = __import__(file[:-3].replace("/", "."), fromlist=["x"])
    cls = getattr(mod, cname)
    func = getattr(cls, mname)
    inner = func
    while hasattr(inner, "__wrapped__"):
        inner = inner.__wrapped__
    code = inner.__code__
    fdef = find_def(code.co_filename, code.co_firstlineno)
    if fdef is None:
        return False
    full = set(job["full"])
    cases = {int(k): v for k, v in job["cases"].items()}
    keep = full | set(cases)
    spans = [(j, (min([s.lineno] + [d.lineno for d in getattr(s, "decorator_list", [])]), s.end_lineno or s.lineno))
             for j, s in enumerate(fdef.body)]
    reduce_method(fdef, keep)
    fdef.name = mname
    cdef = ast.ClassDef(name="_Reduced", bases=[ast.Name("__probe_base", ast.Load())], keywords=[],
                        body=[fdef], decorator_list=[], type_params=[])
    m = ast.Module(body=[cdef], type_ignores=[])
    ast.fix_missing_locations(m)
    ns = dict(vars(mod))
    ns["__probe_base"] = cls
    exec(compile(m, code.co_filename, "exec"), ns)
    red = ns["_Reduced"]
    red.__qualname__ = cls.__qualname__
    rf = getattr(red, mname)
    while hasattr(rf, "__wrapped__"):
        rf = rf.__wrapped__
    CUR["code"], CUR["spans"], CUR["cases"] = rf.__code__, spans, {j: cases[j] for j in cases if j not in full}
    for pool in ("ProcessPoolExecutor", "ThreadPoolExecutor"):
        # setUpClass would start a real pool (a spawn context starts a resource-tracker process
        # even before any submit); this child is disposable, so run submissions inline.
        if isinstance(ns.get(pool), type):
            ns[pool] = mod.__dict__[pool] = lambda *a, **k: SyncExec()
    red.setUpClass()
    try:
        for k, v in list(vars(red).items()) + list(vars(cls).items()):
            if type(v).__name__ in ("ProcessPoolExecutor", "ThreadPoolExecutor"):
                setattr(red, k, SyncExec())
        res = unittest.TestResult()
        red(mname).run(res)
    finally:
        try:
            red.tearDownClass()
            red.doClassCleanups()
        except Exception:
            pass
    ok = res.wasSuccessful() and not res.skipped and res.testsRun == 1
    if not ok and os.environ.get("PROBE_VERBOSE"):
        for _t, tb in (res.failures + res.errors)[:2]:
            print(f"--- {job['id']}\n{tb[-1500:]}", file=sys.stderr)
    return ok


# ---------------------------------------------------------------- in-process use (fork)

_READY = {"src": None, "mods": {}}


def prepare(src: Path) -> None:
    """Once per batch, in the selector: load sqlglot and the test helpers (pinned sources)."""
    if _READY["src"] is not None:
        return
    import site
    if True:
        try:
            site.main()  # the selector runs with -S; tests need site-packages (pytz, duckdb)
        except Exception:
            pass
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path.insert(0, str(src))
    sys.meta_path.insert(0, _LazyFinder())
    import sqlglot  # noqa: F401
    import sqlglot.dialects as dialects
    for name in list(getattr(dialects, "DIALECTS", [])):
        try:
            getattr(dialects, name)  # load every dialect once here, not in each probe
        except Exception:
            pass
    import tests.helpers as helpers
    helpers.load_sql_fixture_pairs = wrap_loader(helpers.load_sql_fixture_pairs)
    helpers.load_sql_fixtures = wrap_loader(helpers.load_sql_fixtures)
    _READY["src"] = src


def preload(files) -> None:
    cwd = os.getcwd()
    os.chdir(_READY["src"])
    try:
        for file in files:
            if file not in _READY["mods"]:
                try:
                    _READY["mods"][file] = __import__(file[:-3].replace("/", "."), fromlist=["x"])
                except BaseException:  # noqa: BLE001 - the child imports it (and fails) itself
                    pass
    finally:
        os.chdir(cwd)


def _codes(code, out):
    out.append(code)
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            _codes(c, out)
    return out


def _literal_defaults(tree):
    """def line (and first decorator line) -> (positional defaults, kw defaults) or None."""
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            try:
                pos = tuple(ast.literal_eval(d) for d in n.args.defaults)
                kw = {a.arg: ast.literal_eval(d) for a, d in zip(n.args.kwonlyargs, n.args.kw_defaults) if d is not None}
                val = (pos, kw)
            except Exception:
                val = None
            for ln in {n.lineno} | {d.lineno for d in getattr(n, "decorator_list", [])}:
                out.setdefault(ln, []).append((ast.dump(n.args), val))
    return out


def _masked(node) -> str:
    import copy

    class Mask(ast.NodeTransformer):
        def visit_Lambda(self, n):
            return ast.Constant(value="<lambda>")
    return ast.dump(Mask().visit(copy.deepcopy(node)))


def _class_assigns(tree):
    out = {}

    def visit(node, path):
        for s in node.body:
            if isinstance(s, ast.ClassDef):
                visit(s, path + [s.name])
            elif path and isinstance(s, (ast.Assign, ast.AnnAssign)) and s.value is not None:
                t = s.targets[0] if isinstance(s, ast.Assign) else s.target
                if isinstance(t, ast.Name):
                    out[(tuple(path), t.id)] = s.value
    visit(tree, [])
    return out


_FUNCS = {"by_file": {}}


def function_index() -> dict:
    """filename -> every live function object (gc.get_objects: ~10 ms, and complete where a walk
    from module namespaces is not, e.g. a lambda held only in another function's __defaults__).
    Rebuilt on each call: imports and lazy caches add functions between probes."""
    import gc
    gc.unfreeze()  # frozen objects are invisible to get_objects
    FT = types.FunctionType
    by_file: dict = {}
    for o in gc.get_objects():
        if type(o) is FT:
            by_file.setdefault(o.__code__.co_filename, []).append(o)
    _FUNCS["by_file"] = by_file
    return by_file


def _code_key(c):
    col = next((p[2] for p in c.co_positions() if p[2] is not None), -1)
    return (c.co_qualname, c.co_firstlineno, col)


def hot_patch(src: Path, overlay: dict, trees: dict | None = None, funcs: dict | None = None) -> None:
    """Make the loaded pinned modules behave like the patched sources (leaf-only edits): swap the
    code of every function object, set changed literal defaults and class constants. Modules not
    loaded yet import from the overlay. Raises when an edit can't be applied exactly."""
    pending = {}
    if funcs is None:  # the forked child gets the parent's index: a rescan would touch every object
        funcs = function_index()
    for rel, new_src in overlay.items():
        name = rel[:-3].replace("/", ".")
        if name.endswith(".__init__"):
            name = name[: -len(".__init__")]
        mod = sys.modules.get(name)
        if mod is None:
            pending[rel] = new_src
            continue
        path = str(src / rel)
        new_codes: dict = {}
        for c in _codes(compile(new_src, path, "exec"), []):
            new_codes.setdefault(_code_key(c), []).append(c)
        if trees and rel in trees:
            old_t, new_t = trees[rel]
        else:
            old_t, new_t = ast.parse((src / rel).read_text()), ast.parse(new_src)
        od, nd = _literal_defaults(old_t), _literal_defaults(new_t)
        dflt = {}
        for ln, lst in nd.items():
            for (dump_new, val), (dump_old, _v) in zip(lst, od.get(ln, [])):
                if dump_new != dump_old:
                    if val is None:
                        raise RuntimeError("non-literal default")
                    dflt[ln] = val
        for obj in funcs.get(path, ()):
            c = obj.__code__
            cands = new_codes.get(_code_key(c))
            if not cands or len(cands) > 1 and len({x for x in cands}) > 1:
                raise RuntimeError(f"no unique new code for {c.co_qualname}:{c.co_firstlineno}")
            if cands[0] != c:
                obj.__code__ = cands[0]
            if c.co_firstlineno in dflt:
                pos, kw = dflt[c.co_firstlineno]
                obj.__defaults__ = pos or None
                obj.__kwdefaults__ = kw or None
        oa, na = _class_assigns(old_t), _class_assigns(new_t)
        for key, node in na.items():
            if key in oa and ast.dump(oa[key]) != ast.dump(node):
                if _masked(oa[key]) == _masked(node):
                    continue  # only lambda bodies changed: their code was swapped above
                path_, attr = key
                holder = mod
                for part in path_:
                    holder = getattr(holder, part)
                # A class constant (literals, or factory calls value_edit allows): evaluate it as
                # the class body would, with the module's globals and the class's own names.
                expr = ast.Expression(body=node)
                ast.fix_missing_locations(expr)
                value = eval(compile(expr, path, "eval"), mod.__dict__, dict(vars(holder)))
                setattr(holder, attr, value)
                if attr == "arg_types" and isinstance(holder, type) and "required_args" in vars(holder):
                    # Expr.__init_subclass__ derived required_args from it when each class was
                    # created: redo that for the class and every subclass inheriting its dict.
                    stack = [holder]
                    while stack:
                        k = stack.pop()
                        k.required_args = {a for a, req in k.arg_types.items() if req}
                        stack.extend(c for c in k.__subclasses__() if "arg_types" not in vars(c))
    if pending:
        sys.meta_path.insert(0, Overlay(src, pending))


def run_forked(job: dict, deadline_s: float) -> tuple[dict, float]:
    """Fork, hot-patch in the child, run the reduced tests; results stream through a pipe.
    Returns ({test id: passed}, child CPU seconds)."""
    import select
    import time
    src = _READY["src"]
    preload({t["id"].split("::")[0] for t in job["tests"]})
    funcs = function_index()
    sys.stdout.flush()
    sys.stderr.flush()
    gc_mode = os.environ.get("PROBE_GC", "freeze")
    if gc_mode != "plain":
        # Without this the child's first full collection walks (and refcount-touches) the whole
        # inherited heap: copy-on-write faults on nearly every page, charged as child CPU.
        import gc
        gc.collect()
        gc.freeze()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # child
        code = 0
        if gc_mode != "plain":
            import gc
            gc.disable()  # short-lived, memory-capped by the run; cycles die with the process
        try:
            os.close(r)
            os.chdir(src)
            sys.stdout = sys.stderr
            _t0 = time.process_time()
            hot_patch(src, job["overlay"], job.get("trees"), funcs)
            if os.environ.get("PROBE_VERBOSE"):
                print(f"PATCH {time.process_time() - _t0:.3f}", file=sys.stderr)
            for t in job["tests"]:
                _t1 = time.process_time()
                try:
                    ok = run_one(t, _READY["mods"])
                    if os.environ.get("PROBE_VERBOSE"):
                        print(f"TIME {time.process_time() - _t1:.3f} {t['id']}", file=sys.stderr)
                except BaseException as e:  # noqa: BLE001
                    print(f"probe {t['id']}: {type(e).__name__}: {e}", file=sys.stderr)
                    ok = False
                os.write(w, (json.dumps({t["id"]: ok}) + "\n").encode())
                if not ok and job.get("failfast"):
                    break  # the commit breaks something: the rest rarely lowers its cost
        except BaseException as e:  # noqa: BLE001
            print(f"probe child: {type(e).__name__}: {e}", file=sys.stderr)
            code = 1
        finally:
            os._exit(code)
    os.close(w)
    tick = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    stat = f"/proc/{pid}/stat"

    def child_cpu():
        try:
            with open(stat) as fh:
                f = fh.read().rsplit(")", 1)[1].split()
            return (int(f[11]) + int(f[12])) / tick
        except (OSError, IndexError, ValueError):
            return None

    # The budget is the child's CPU (what the score charges); wall time is only a backstop.
    buf, end = b"", time.monotonic() + 3 * deadline_s + 2
    killed = False
    while True:
        left = end - time.monotonic()
        cpu = child_cpu()
        if left <= 0 or (cpu is not None and cpu > deadline_s):
            os.kill(pid, 9)
            killed = True
            break
        ready, _, _ = select.select([r], [], [], min(left, 0.05))
        if ready:
            chunk = os.read(r, 65536)
            if not chunk:
                break
            buf += chunk
    os.close(r)
    _, status, ru = os.wait4(pid, 0)
    res = {}
    for line in buf.decode().splitlines():
        try:
            res.update(json.loads(line))
        except ValueError:
            pass
    if killed:
        print("stmt probe child over budget", file=sys.stderr)
    return res, ru.ru_utime + ru.ru_stime


def main():
    src = Path(sys.argv[1]).resolve()
    job = json.loads(Path(sys.argv[2]).read_text())
    os.environ["SKIP_INTEGRATION"] = "0"
    os.chdir(src)
    sys.path.insert(0, str(src))
    sys.meta_path.insert(0, _LazyFinder())
    sys.meta_path.insert(0, Overlay(src, job["overlay"]))
    import tests.helpers as helpers
    helpers.load_sql_fixture_pairs = wrap_loader(helpers.load_sql_fixture_pairs)
    helpers.load_sql_fixtures = wrap_loader(helpers.load_sql_fixtures)
    mods = {}
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    import time as _time
    for t in job["tests"]:
        _c0 = _time.process_time()
        try:
            ok = run_one(t, mods)
        except BaseException as e:  # noqa: BLE001
            print(f"probe {t['id']}: {type(e).__name__}: {e}", file=sys.stderr)
            ok = False
        if os.environ.get("PROBE_VERBOSE"):
            print(f"TIME {_time.process_time() - _c0:.3f} {t['id']}", file=sys.stderr)
        real_stdout.write(json.dumps({t["id"]: ok}) + "\n")
        real_stdout.flush()


if __name__ == "__main__":
    main()
