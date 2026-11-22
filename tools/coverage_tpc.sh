#!/bin/sh
# Exact coverage of the two TPC executor tests, whose work runs in a spawn process pool that
# coverage can't see. Runs them on a copy in /tmp/s:
#   - every sqlglot module is imported at collection (conftest), so import-time lines get the
#     empty context instead of being credited to whichever test loaded a dialect lazily;
#   - the pool is replaced by a synchronous one (work runs in the calling thread, in order:
#     coverage contexts are process-wide, so nothing may run concurrently);
#   - parse_one/transpile in the test and optimize() in the executor switch to the context
#     "tpc-probed" while they run: lines the test reaches only through them are the ones the
#     selector's TPC probe (tools/tpc_probe.py) compares exactly. Everything else the test runs
#     keeps the test's own context.
# Writes work/sqlglot/.coverage.tpc.
#   docker run --rm -v "$PWD":/w -w /w python:3.13-slim sh -c "pip install -q -r requirements.lock pytest-cov && sh tools/coverage_tpc.sh"
set -e
rm -rf /tmp/s && mkdir /tmp/s && cp -r work/sqlglot/sqlglot work/sqlglot/tests /tmp/s/
cd /tmp/s
python - <<'PY'
from pathlib import Path
p = Path("tests/test_executor.py"); s = p.read_text()
old = 'ProcessPoolExecutor(\n            mp_context=get_context("spawn"),'
assert old in s
s = s.replace(old, '_SyncPool(')
s += '''

import concurrent.futures as _cf
import os as _os

import coverage as _coverage
import sqlglot.executor as _executor


class _SyncPool:
    def __init__(self, initializer=None, initargs=()):
        initializer(*initargs)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def submit(self, fn, *args):
        f = _cf.Future()
        try:
            f.set_result(fn(*args))
        except BaseException as e:
            f.set_exception(e)
        return f


def _probed(fn):
    def wrapper(*args, **kwargs):
        cov = _coverage.Coverage.current()
        test = _os.environ.get("PYTEST_CURRENT_TEST", "").rsplit(" ", 1)[0]
        if cov is None or not test:
            return fn(*args, **kwargs)
        cov.switch_context("tpc-probed")
        try:
            return fn(*args, **kwargs)
        finally:
            cov.switch_context(test + "|run")
    return wrapper


parse_one = _probed(parse_one)
transpile = _probed(transpile)
_executor.optimize = _probed(_executor.optimize)
'''
p.write_text(s)
Path("tests/conftest.py").write_text("""
import importlib, pkgutil, sqlglot
for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    try:
        importlib.import_module(m.name)
    except Exception:
        pass
""")
PY
COVERAGE_FILE=.coverage.tpc SKIP_INTEGRATION=0 PYTHONPATH=/tmp/s python -m pytest -q -p no:cacheprovider \
  --cov=sqlglot --cov-context=test --cov-report= \
  tests/test_executor.py::TestExecutor::test_execute_tpch tests/test_executor.py::TestExecutor::test_execute_tpcds 2>&1 | tail -2
cp .coverage.tpc /w/work/sqlglot/.coverage.tpc
