"""What test_execute_tpcds feeds the parts of itself that run unchanged code, as fingerprints.

    python tpc_probe.py SRC [OVERLAY.json [BASE.json]]  ->  stdout: {"h": [...], "t": s, "x": {...}}
    python tpc_probe.py SRC --baseline                   ->  also "x" and "xt" for every query

For every `# execute: true` query of tests/fixtures/optimizer/tpc-ds/tpc-ds.sql it records what
the test computes with sqlglot before handing over to code the probe can't run cheaply: the
parsed tree (parse_one, which the parent pickles to the child), the optimized tree (execute() ->
optimize(..., leave_tables_isolated=True), which the planner and the Python executor consume) and
the DuckDB SQL the expected results come from (transpile(..., write="duckdb")). Trees are
fingerprinted with serde.dump: classes, args, types, comments and meta, everything pickling sends.

Where a fingerprint differs from BASE, the probe can still settle that query by doing what the
child does: execute() on the TPC-DS tables (loaded as the test's setUpClass loads them), result
fingerprinted ("x": index -> hash). The build records every query's result and time with the
pinned code, whose results match DuckDB's (the test passes there), so an equal result means the
query's subtest passes. Only a few cheap queries are executed (EXEC_BUDGET seconds of the build's
own timings); the rest stay unsettled.

OVERLAY maps repo paths ("sqlglot/optimizer/normalize.py") to patched sources, served by an import
hook in place of the files under SRC. The image runs it once at build time on the pinned sources;
the selector runs it with a commit's patched files and drops test_execute_tpcds only when every
fingerprint matches and the rest of what the test runs never touches the edited lines
(see Selector.tpc_probe_clears).
"""

import hashlib
import importlib.abc  # run with PYTHONHASHSEED=0 (the selector and the image do): stable row orders
import importlib.util
import json
import sys
import time
from pathlib import Path


class Overlay(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, root: Path, sources: dict[str, str]):
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


EXEC_BUDGET = 4.0  # seconds of baseline execution time the runtime probe may spend
EXEC_MAX = 4  # differing queries it may execute


def load_table(root: Path, table: str, columns):
    """tests/test_executor.py setUpClass's loader for one TPC-DS table."""
    import ast as _ast
    import csv
    import gzip

    from sqlglot.executor.table import Table

    path = root / "tests/fixtures/optimizer/tpc-ds" / f"{table}.csv.gz"
    with open(path, "rb") as fh:
        gz = fh.read(2) == b"\x1f\x8b"
    fh = gzip.open(path, "rt", newline="") if gz else open(path, encoding="utf-8", newline="")
    with fh:
        reader = csv.reader(fh, delimiter="|")
        next(reader)
        rows, ctypes = [], []
        for row in reader:
            if not ctypes:
                for v in row:
                    try:
                        ctypes.append(type(_ast.literal_eval(v)))
                    except (ValueError, SyntaxError):
                        ctypes.append(str)
            rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
    return Table(columns=columns, rows=rows)


def run_query(root: Path, sql: str, schema_dict, cache: dict) -> str:
    """mp_execute(): the query's tables, then execute(); the result as a fingerprint."""
    from sqlglot import find_tables, parse_one
    from sqlglot.executor import execute

    try:
        expression = parse_one(sql)
        tables = {}
        for t in find_tables(expression):
            if t.name not in cache:
                cache[t.name] = load_table(root, t.name, schema_dict[t.name])
            tables[t.name] = cache[t.name]
        result = execute(expression, schema=schema_dict, tables=tables)
        blob = repr((tuple(result.columns), [tuple(r) for r in result.rows]))
    except Exception as e:  # noqa: BLE001
        blob = f"error {type(e).__name__}: {e}"
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def fingerprint(sql: str) -> str:
    """parse_one, optimize and transpile(write="duckdb") of one query, as one hash."""
    from sqlglot import parse_one, transpile
    from sqlglot.optimizer import optimize
    from sqlglot.schema import ensure_schema
    from sqlglot.serde import dump
    from tests.helpers import TPCDS_SCHEMA

    parts = []
    try:
        parsed = parse_one(sql)
        parts.append(dump(parsed))
        schema = ensure_schema(TPCDS_SCHEMA, dialect=None)
        parts.append(dump(optimize(parsed, schema, leave_tables_isolated=True, dialect=None)))
    except Exception as e:  # noqa: BLE001 - the test would error the same way
        parts.append(f"error {type(e).__name__}: {e}")
    try:
        parts.append(transpile(sql, write="duckdb")[0])
    except Exception as e:  # noqa: BLE001
        parts.append(f"error {type(e).__name__}: {e}")
    blob = json.dumps(parts, sort_keys=True, default=repr)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def query_coverage(root: Path, sqls: list[str]) -> dict:
    """Build time: for each query, the sqlglot lines fingerprint() runs, each query in a fresh fork
    of a process that has only imported sqlglot. Cold, so a query that would read lazily cached
    state (dispatch tables, per-type caches) runs the code that computes it and shows it here.
    -> {repo path: {line: bitmask of query indexes}}"""
    import os
    prefix = str(root / "sqlglot") + "/"
    cut = len(str(root)) + 1
    cov: dict = {}
    for i, sql in enumerate(sqls):
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            mon = sys.monitoring
            hits = set()

            def on_line(code, line):
                f = code.co_filename
                if f.startswith(prefix):
                    hits.add((f[cut:], line))
                return mon.DISABLE

            mon.use_tool_id(mon.COVERAGE_ID, "tpc-qcov")
            mon.register_callback(mon.COVERAGE_ID, mon.events.LINE, on_line)
            mon.set_events(mon.COVERAGE_ID, mon.events.LINE)
            fingerprint(sql)
            mon.set_events(mon.COVERAGE_ID, 0)
            with os.fdopen(w, "w") as fh:
                json.dump(sorted(hits), fh)
            os._exit(0)
        os.close(w)
        with os.fdopen(r) as fh:
            data = fh.read()
        _, status = os.waitpid(pid, 0)
        if status != 0:
            raise RuntimeError(f"coverage of query {i} failed")
        for f, ln in json.loads(data):
            d = cov.setdefault(f, {})
            d[ln] = d.get(ln, 0) | (1 << i)
    return cov


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    baseline = len(sys.argv) > 2 and sys.argv[2] == "--baseline"
    qcov = len(sys.argv) > 2 and sys.argv[2] == "--qcov"
    sources = json.loads(Path(sys.argv[2]).read_text()) if len(sys.argv) > 2 and not (baseline or qcov) else {}
    base = json.loads(Path(sys.argv[3]).read_text()) if len(sys.argv) > 3 else None
    # Optional 4th argument: comma-separated query indexes to recompute; the others keep BASE's
    # fingerprint (the selector passes the queries whose cold coverage reaches the edit).
    only = None
    if len(sys.argv) > 4 and base is not None:
        only = {int(x) for x in sys.argv[4].split(",") if x}
    sys.path.insert(0, str(root))
    if sources:
        sys.meta_path.insert(0, Overlay(root, sources))
    start = time.process_time()
    import sqlglot  # noqa: F401
    from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs

    sqls = [sql for meta, sql, _ in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql") if meta.get("execute")]
    if qcov:
        print(json.dumps({"n": len(sqls), "files": query_coverage(root, sqls)}))
        return
    if only is not None and len(base["h"]) == len(sqls):
        out = [fingerprint(sql) if i in only else base["h"][i] for i, sql in enumerate(sqls)]
    else:
        out = [fingerprint(sql) for sql in sqls]
    result = {"h": out, "t": round(time.process_time() - start, 3)}
    cache: dict = {}
    if baseline:
        result["x"], result["xt"] = {}, {}
        for i, sql in enumerate(sqls):
            t0 = time.process_time()  # tables loaded afresh per query: what a runtime probe pays
            result["x"][str(i)] = run_query(root, sql, TPCDS_SCHEMA, {})
            result["xt"][str(i)] = round(time.process_time() - t0, 3)
    elif base is not None and "x" in base and len(base["h"]) == len(out):
        differ = [i for i, (a, b) in enumerate(zip(out, base["h"])) if a != b]
        cost = sum(base["xt"][str(i)] for i in differ)
        if differ and len(differ) <= EXEC_MAX and cost <= EXEC_BUDGET:
            result["x"] = {str(i): run_query(root, sqls[i], TPCDS_SCHEMA, cache) for i in differ}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
