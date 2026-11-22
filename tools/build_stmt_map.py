"""Dev/build: which top-level statement (and which fixture case) of every test runs each sqlglot line.

    python build_stmt_map.py SRC TESTS_JSON OUT_DIR [SHARD NSHARDS]
    python build_stmt_map.py --merge OUT_DIR MAP_OUT

Runs every test of tests.json (pinned sources, SKIP_INTEGRATION=0) in-process under
sys.monitoring. A context is (statement index of the test method's body, fixture case or None):
  - the test method's own LINE events say which top-level statement is running;
  - tests.helpers.load_sql_fixture_pairs / load_sql_fixtures items set the case (file, k) while
    the consumer handles them, and subTest(title) blocks whose title names a case do the same
    (check_file evaluates in a result loop after all items were read);
  - setUpClass / setUp / tearDown / cleanups are context -1 (a hit there makes the test
    unprobeable).
Process pools held by test classes are replaced by a synchronous executor, so every case's work
happens in this process, inside its own context.

Per test the map stores, for every sqlglot line it ran, the set of contexts that ran it (bitsets
deduplicated), each context's seconds, and whether the test passed at the pin.
"""

import ast
import contextlib
import json
import marshal
import os
import sys
import time
import unittest
import zlib
from concurrent.futures import Future
from pathlib import Path

SETUP = -1


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
        return map(fn, *its)


class State:
    def __init__(self, src_prefix):
        self.prefix = src_prefix
        self.files: dict[str, int] = {}
        self.code = None
        self.spans: list[tuple[int, int]] = []
        self.stmt = SETUP
        self.case = None
        self.hits: dict[tuple, set] = {}
        self.times: dict[tuple, float] = {}
        self.last = time.perf_counter()
        self.titles: dict[tuple, set] = {}

    @property
    def ctx(self):
        return (self.stmt, self.case)

    def switch(self, stmt, case):
        now = time.perf_counter()
        c = self.ctx
        self.times[c] = self.times.get(c, 0.0) + now - self.last
        self.last = now
        self.stmt, self.case = stmt, case
        sys.monitoring.restart_events()


ST: State = None  # type: ignore
TOOL = 4
DISABLE = sys.monitoring.DISABLE


def on_line(code, line):
    st = ST
    if code is st.code:
        i = -2
        for j, (a, b) in enumerate(st.spans):
            if a <= line <= b:
                i = j
                break
        if i == -2:
            return None  # a line outside every body statement (decorator re-eval etc.)
        if i != st.stmt:
            st.switch(i, None)
        return None
    fn = code.co_filename
    if fn.startswith(st.prefix):
        fid = st.files.get(fn)
        if fid is None:
            fid = st.files[fn] = len(st.files)
        st.hits.setdefault(st.ctx, set()).add(fid * 100000 + line)
    return DISABLE


def wrap_loader(orig, pairs: bool):
    def loader(filename):
        stmt = None
        for k, item in enumerate(orig(filename), 1):
            if stmt is None:
                stmt = ST.stmt
            if ST.stmt == stmt:
                case = (filename, k)
                if pairs:
                    meta, sql, _exp = item
                    for t in {meta.get("title") or f"{k}, {sql}", f"{k}, {sql}"}:
                        ST.titles.setdefault((stmt, t), set()).add(case)
                ST.switch(stmt, case)
            yield item
        if stmt is not None and ST.stmt == stmt:
            ST.switch(stmt, None)
    loader.__wrapped_loader__ = orig
    return loader


_orig_subtest = unittest.TestCase.subTest


@contextlib.contextmanager
def subtest(self, msg=None, **params):
    key = msg if isinstance(msg, str) else None
    if key and key.startswith("(execute) "):
        key = key[len("(execute) "):]
    cases = ST.titles.get((ST.stmt, key)) if key else None
    prev = ST.case
    if cases and len(cases) == 1:
        ST.switch(ST.stmt, next(iter(cases)))
    try:
        if msg is None:
            with _orig_subtest(self, **params):
                yield
        else:
            with _orig_subtest(self, msg, **params):
                yield
    finally:
        if cases and len(cases) == 1:
            ST.switch(ST.stmt, prev)


def method_spans(func) -> list[tuple[int, int]] | None:
    code = func.__code__
    try:
        tree = ast.parse(Path(code.co_filename).read_text())
    except Exception:
        return None
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            first = min([n.lineno] + [d.lineno for d in n.decorator_list])
            if first == code.co_firstlineno or n.lineno == code.co_firstlineno:
                spans = []
                for s in n.body:
                    a = min([s.lineno] + [d.lineno for d in getattr(s, "decorator_list", [])])
                    spans.append((a, s.end_lineno or s.lineno))
                return spans
    return None


def unwrap(f):
    while hasattr(f, "__wrapped__"):
        f = f.__wrapped__
    return f


def encode(st: State, ok: bool):
    """{line: contexts} with deduplicated bitsets -> compact marshal blob."""
    ctxs = sorted(set(st.hits) | set(st.times), key=lambda c: (c[0], str(c[1])))
    idx = {c: i for i, c in enumerate(ctxs)}
    by_line: dict[int, int] = {}
    for c, lines in st.hits.items():
        bit = 1 << idx[c]
        for ln in lines:
            by_line[ln] = by_line.get(ln, 0) | bit
    masks: dict[int, int] = {}
    lines_out, mask_ids = [], []
    for ln in sorted(by_line):
        m = by_line[ln]
        mid = masks.setdefault(m, len(masks))
        lines_out.append(ln)
        mask_ids.append(mid)
    mask_list = [None] * len(masks)
    for m, i in masks.items():
        mask_list[i] = m
    deltas = [lines_out[0]] + [b - a for a, b in zip(lines_out, lines_out[1:])] if lines_out else []
    ctx_out = [(c[0], c[1][0] if c[1] else None, c[1][1] if c[1] else None, round(st.times.get(c, 0.0), 4))
               for c in ctxs]
    return {"ok": ok, "ctx": ctx_out, "masks": mask_list, "d": deltas, "m": mask_ids}


def run(src: Path, tests_json: Path, out_dir: Path, shard: int, nshards: int):
    global ST
    os.environ["SKIP_INTEGRATION"] = "0"
    os.chdir(src)
    sys.path.insert(0, str(src))
    tests = json.loads(tests_json.read_text())["tests"]
    by_class: dict[tuple[str, str], list[dict]] = {}
    for t in tests:
        parts = t["id"].split("::")
        if len(parts) != 3:
            continue
        by_class.setdefault((t["file"], parts[1]), []).append(t)
    keys = sorted(by_class)[shard::nshards]
    ST = State(str(src / "sqlglot") + "/")
    import tests.helpers as helpers  # noqa: E402

    lp, lf = helpers.load_sql_fixture_pairs, helpers.load_sql_fixtures
    wp, wf = wrap_loader(lp, True), wrap_loader(lf, False)
    helpers.load_sql_fixture_pairs, helpers.load_sql_fixtures = wp, wf
    unittest.TestCase.subTest = subtest
    mon = sys.monitoring
    mon.use_tool_id(TOOL, "stmtmap")
    mon.register_callback(TOOL, mon.events.LINE, on_line)
    out = {}
    for file, cname in keys:
        mod_name = file[:-3].replace("/", ".")
        try:
            mod = __import__(mod_name, fromlist=["x"])
        except Exception as e:
            print(f"import {mod_name} failed: {e}", file=sys.stderr)
            continue
        for name, val in list(vars(mod).items()):
            if val is lp:
                setattr(mod, name, wp)
            elif val is lf:
                setattr(mod, name, wf)
        cls = getattr(mod, cname, None)
        if cls is None:
            continue
        class_hits: dict = {}
        ST.code, ST.spans = None, []
        ST.hits, ST.times = {}, {}
        ST.stmt, ST.case = SETUP, None
        mon.set_events(TOOL, mon.events.LINE)
        setup_ok = True
        try:
            cls.setUpClass()
        except Exception as e:
            print(f"setUpClass {cname} failed: {e}", file=sys.stderr)
            setup_ok = False
        mon.set_events(TOOL, 0)
        class_hits = ST.hits.get((SETUP, None), set())
        for k, v in list(vars(cls).items()):
            if type(v).__name__ in ("ProcessPoolExecutor", "ThreadPoolExecutor"):
                setattr(cls, k, SyncExec())
        for t in by_class[(file, cname)]:
            mname = t["id"].split("::")[2]
            func = getattr(cls, mname, None)
            if func is None:
                continue
            inner = unwrap(func)
            spans = method_spans(inner) or []
            ST.code, ST.spans = inner.__code__, spans
            ST.hits = {(SETUP, None): set(class_hits)}
            ST.times = {}
            ST.titles = {}
            ST.stmt, ST.case = SETUP, None
            ST.last = time.perf_counter()
            inst = cls(mname)
            td, dc = inst.tearDown, inst.doCleanups

            def tear(td=td):
                ST.switch(SETUP, None)
                td()

            def clean(dc=dc):
                ST.switch(SETUP, None)
                return dc()

            inst.tearDown, inst.doCleanups = tear, clean
            res = unittest.TestResult()
            mon.restart_events()
            mon.set_events(TOOL, mon.events.LINE)
            try:
                inst.run(res)
            finally:
                mon.set_events(TOOL, 0)
                ST.switch(SETUP, None)
            ok = setup_ok and res.wasSuccessful() and not res.skipped
            if not spans:
                ok = False
            out[t["id"]] = encode(ST, ok)
            if not res.wasSuccessful():
                print(f"FAIL {t['id']}: {[str(x[0]) for x in res.failures + res.errors][:2]}", file=sys.stderr)
        try:
            cls.tearDownClass()
            cls.doClassCleanups()
        except Exception:
            pass
    files = {v: k[len(str(src)) + 1:] for k, v in ST.files.items()}
    (out_dir / f"part{shard}.marshal").write_bytes(marshal.dumps({"files": files, "tests": out}))
    print(f"shard {shard}: {len(out)} tests", file=sys.stderr)


def merge(out_dir: Path, dest: Path):
    """Re-number files globally; one zlib blob per test."""
    files: dict[str, int] = {}
    tests = {}
    for p in sorted(out_dir.glob("part*.marshal")):
        d = marshal.loads(p.read_bytes())
        remap = {}
        for fid, rel in d["files"].items():
            remap[fid] = files.setdefault(rel, len(files))
        for tid, e in d["tests"].items():
            lines = []
            acc = 0
            for x in e["d"]:
                acc += x
                lines.append(acc)
            lines = [remap[ln // 100000] * 100000 + ln % 100000 for ln in lines]
            order = sorted(range(len(lines)), key=lines.__getitem__)
            lines = [lines[i] for i in order]
            mids = [e["m"][i] for i in order]
            deltas = [lines[0]] + [b - a for a, b in zip(lines, lines[1:])] if lines else []
            blob = {"ok": e["ok"], "ctx": e["ctx"], "masks": e["masks"], "d": deltas, "m": mids}
            tests[tid] = zlib.compress(marshal.dumps(blob), 9)
    data = {"files": files, "tests": tests}
    dest.write_bytes(zlib.compress(marshal.dumps(data), 1))
    print(f"{len(tests)} tests, {dest.stat().st_size / 1e6:.1f} MB", file=sys.stderr)


if __name__ == "__main__":
    if sys.argv[1] == "--merge":
        merge(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        shard, n = (int(sys.argv[4]), int(sys.argv[5])) if len(sys.argv) > 5 else (0, 1)
        out_dir = Path(sys.argv[3]).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        run(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), out_dir, shard, n)
