#!/bin/sh
# Same fix as coverage_optimizer.sh for the two TPC executor tests.
set -e
pip install -q -r requirements.lock pytest-cov coverage 2>&1 | grep -v notice || true
cd work/sqlglot
cp tests/test_executor.py /tmp/orig.py
python - <<'PY'
from pathlib import Path
p = Path("tests/test_executor.py"); s = p.read_text()
s = s.replace('ProcessPoolExecutor(\n            mp_context=get_context("spawn"),', '__import__("concurrent.futures").futures.ThreadPoolExecutor(max_workers=1,')
assert "ThreadPoolExecutor" in s
p.write_text(s)
PY
COVERAGE_FILE=.coverage.exec SKIP_INTEGRATION=0 PYTHONPATH=. python -m pytest -q -p no:cacheprovider -n 2 \
  --cov=sqlglot --cov-context=test --cov-report= "tests/test_executor.py::TestExecutor::test_execute_tpch" "tests/test_executor.py::TestExecutor::test_execute_tpcds" > ../pytest_exec.log 2>&1 || true
cp /tmp/orig.py tests/test_executor.py
tail -2 ../pytest_exec.log
