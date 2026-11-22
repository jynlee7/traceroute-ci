"""Turns per-test coverage of sqlglot at the pinned commit into maps/impact.json.gz.

Run inside the coverage container after tools/coverage.sh:
    docker run --rm -v "$PWD":/w -w /w python:3.13-slim sh -c \
        "pip install -q coverage && python tools/build_map.py"

Everything here is derived from the pinned commit only (coverage, test sources,
function ranges). Nothing is derived from patches or answers.
"""

import ast
import gzip
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

from coverage import CoverageData

ROOT = Path("work/sqlglot")
OUT = Path("maps/impact.json.gz")


def test_ids() -> list[str]:
    tests = json.loads(Path("sample/tests.json").read_text())["tests"]
    return [t["id"] for t in tests]


def function_ranges(source: str) -> list[list[int]]:
    """[start, end] of every function and class, decorators included, innermost last."""
    ranges = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            kind = 1 if isinstance(node, ast.ClassDef) else 0
            ranges.append([start, node.end_lineno, kind])
    ranges.sort(key=lambda r: (r[0], -r[1]))
    return ranges


def test_strings(path: Path) -> dict[str, set[str]]:
    """Test id -> every string constant inside that test (and its class's helpers)."""
    rel = str(path.relative_to(ROOT))
    tree = ast.parse(path.read_text())
    out: dict[str, set[str]] = {}

    def consts(node) -> set[str]:
        return {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)}

    module_consts = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test"):
            out[f"{rel}::{node.name}"] = consts(node)
        elif isinstance(node, ast.ClassDef):
            helper = set()
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and not item.name.startswith("test"):
                    helper |= consts(item)
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name.startswith("test"):
                    out[f"{rel}::{node.name}::{item.name}"] = consts(item) | helper
        else:
            module_consts |= consts(node)
    return {k: v | module_consts for k, v in out.items()}


def main() -> None:
    ids = test_ids()
    index = {tid: i for i, tid in enumerate(ids)}
    data = CoverageData(basename=str(ROOT / ".coverage"))
    data.read()
    # Re-runs of tests whose work happens in process pools (see tools/coverage_*.sh).
    for extra in (".coverage.opt", ".coverage.exec"):
        if (ROOT / extra).exists():
            more = CoverageData(basename=str(ROOT / extra))
            more.read()
            data.update(more)
            print(f"merged {extra}", file=sys.stderr)

    set_table: list[list[int]] = []
    set_ids: dict[tuple, int] = {}

    def intern(s) -> int:
        key = tuple(sorted(s))
        if key not in set_ids:
            set_ids[key] = len(set_table)
            set_table.append(list(key))
        return set_ids[key]

    # setUpClass runs in the setup phase of whichever test of the class comes first, but its work
    # (tables, schemas, parsed fixtures) is shared by every test of the class: a mutant there errors
    # them all. Give lines seen in a "|setup" context to the whole class.
    class_tests: dict[str, set[int]] = defaultdict(set)
    for tid, i in index.items():
        if tid.count("::") == 2:
            class_tests[tid.rsplit("::", 1)[0]].add(i)

    files = {}
    unmatched = set()
    for measured in sorted(data.measured_files()):
        # Paths are recorded where coverage ran (e.g. /w/work/sqlglot/... in the container).
        marker = "/work/sqlglot/"
        if marker in measured:
            rel = measured.split(marker, 1)[1]
        elif measured.startswith("/tmp/s/"):  # tools/coverage_tpc.sh runs on a copy in /tmp/s
            rel = measured[len("/tmp/s/"):]
        else:
            rel = os.path.relpath(measured, ROOT.resolve())
        if not rel.startswith("sqlglot/"):
            continue
        lines = {}
        imported = []  # lines that ran outside any test: at import / collection time
        for lineno, contexts in data.contexts_by_lineno(measured).items():
            tests = set()
            if "" in contexts:
                imported.append(lineno)
            for ctx in contexts:
                if not ctx:
                    continue
                tid, _, phase = ctx.rpartition("|")
                if not tid:
                    tid, phase = ctx, ""
                if tid in index:
                    tests.add(index[tid])
                    if phase == "setup" and tid.count("::") == 2:
                        tests |= class_tests[tid.rsplit("::", 1)[0]]
                else:
                    unmatched.add(tid)
            lines[str(lineno)] = intern(tests)
        files[rel] = {"lines": lines, "imp": sorted(imported)}

    for path in sorted((ROOT / "sqlglot").rglob("*.py")):
        rel = str(path.relative_to(ROOT))
        entry = files.setdefault(rel, {"lines": {}})
        entry["ranges"] = function_ranges(path.read_text())

    # Tests that send expressions to spawned process pools pickle them through
    # Expression.__reduce__ -> sqlglot.serde. Coverage re-runs use threads, which skip that path.
    pool_tests = {i for i, tid in enumerate(ids) if tid.startswith("tests/test_optimizer.py::")}
    pool_tests |= {index[t] for t in ("tests/test_executor.py::TestExecutor::test_execute_tpch",
                                       "tests/test_executor.py::TestExecutor::test_execute_tpcds") if t in index}
    pickle_lines: dict[str, range] = {}
    serde_src = (ROOT / "sqlglot/serde.py").read_text()
    pickle_lines["sqlglot/serde.py"] = range(1, serde_src.count("\n") + 2)
    core = ast.parse((ROOT / "sqlglot/expressions/core.py").read_text())
    for node in ast.walk(core):
        if isinstance(node, ast.FunctionDef) and node.name in ("__reduce__", "__reduce_ex__", "__getstate__", "__setstate__"):
            pickle_lines["sqlglot/expressions/core.py"] = range(node.lineno, node.end_lineno + 1)
    for rel, rng in pickle_lines.items():
        lines = files.setdefault(rel, {"lines": {}})["lines"]
        for ln in rng:
            old = set(set_table[lines[str(ln)]]) if str(ln) in lines else set()
            lines[str(ln)] = intern(old | pool_tests)

    # test_execute_tpch/tpcds run execute() (optimizer + planner + Python executor) on the TPC
    # queries inside spawned processes, which coverage misses. Give them the coverage of the
    # in-process tests that walk the same paths, plus all executor and planner code.
    proxies = ["tests/test_executor.py::TestExecutor::test_optimized_tpch",
               "tests/test_optimizer.py::TestOptimizer::test_tpch",
               "tests/test_optimizer.py::TestOptimizer::test_tpcds"]
    proxy_ids = {index[p] for p in proxies if p in index}
    # Only TPC-DS executes in the pool: tpc-h.sql has no "# execute: true" entries, so
    # test_execute_tpch's children return None and its own (parent-side) coverage is exact.
    tpc_exec = {index[t] for t in ("tests/test_executor.py::TestExecutor::test_execute_tpcds",) if t in index}
    # Exact coverage of the TPC executor tests (tools/coverage_tpc.sh: the same tests with the
    # spawn pool swapped for one thread, so coverage sees the children's work). When present it
    # replaces the proxy below: the proxy both missed lines the children run (simplify, builders,
    # jsonpath...) and claimed whole files they never touch.
    exact_path = ROOT / ".coverage.tpc"
    if exact_path.exists() and tpc_exec:
        tpc = CoverageData(basename=str(exact_path))
        tpc.read()
        added = 0
        for measured in tpc.measured_files():
            rel = measured[len("/tmp/s/"):] if measured.startswith("/tmp/s/") else \
                measured.split("/work/sqlglot/", 1)[-1]
            if not rel.startswith("sqlglot/"):
                continue
            entry = files.setdefault(rel, {"lines": {}})
            lines = entry["lines"]
            # Only these two tests ran in that process, so modules imported lazily during them
            # (dialects) credit their import-time lines (class bodies, def lines) to tpcds. Lines
            # the main run executed at import are left to the import-time rules.
            imported = set(entry.get("imp", ()))
            outside = []  # lines tpcds runs outside parse_one/transpile/optimize (the probe can't see)
            for ln, contexts in tpc.contexts_by_lineno(measured).items():
                own = any(c.startswith("tests/test_executor.py::TestExecutor::test_execute_tpcds") for c in contexts)
                if own:
                    outside.append(ln)
                if ln in imported:
                    continue
                if own or "tpc-probed" in contexts:
                    old = set(set_table[lines[str(ln)]]) if str(ln) in lines else set()
                    lines[str(ln)] = intern(old | tpc_exec)
                    added += 1
            entry["tpc_out"] = sorted(outside)
        print(f"exact TPC coverage: {added} lines", file=sys.stderr)
        proxy_ids = set()  # no proxy
    for rel, entry in files.items():
        if not proxy_ids:
            break
        lines = entry["lines"]
        # The children run the planner, the Python executor and the Python generator it uses.
        whole = rel.startswith("sqlglot/executor/") or rel in ("sqlglot/planner.py", "sqlglot/generators/python.py")
        if whole:
            src = (ROOT / rel).read_text()
            for ln in range(1, src.count("\n") + 2):
                lines.setdefault(str(ln), intern(set()))
        for ln, sid in list(lines.items()):
            members = set(set_table[sid])
            if whole or members & proxy_ids:
                lines[ln] = intern(members | tpc_exec)

    strings: dict[str, list[str]] = {}
    test_ranges: dict[str, list] = {}
    for path in sorted((ROOT / "tests").rglob("*.py")):
        rel = str(path.relative_to(ROOT))
        test_ranges[rel] = function_ranges(path.read_text())
        for tid, s in test_strings(path).items():
            if tid in index:
                strings[str(index[tid])] = sorted(x for x in s if 3 <= len(x) <= 200)

    OUT.parent.mkdir(exist_ok=True)
    payload = {"tests": ids, "sets": set_table, "files": files, "test_strings": strings, "test_ranges": test_ranges}
    with gzip.open(OUT, "wt", compresslevel=9) as fh:
        json.dump(payload, fh, separators=(",", ":"))
    print(f"files={len(files)} sets={len(set_table)} unmatched={len(unmatched)} size={OUT.stat().st_size}", file=sys.stderr)
    for tid in sorted(unmatched)[:5]:
        print("unmatched", tid, file=sys.stderr)


if __name__ == "__main__":
    main()
