#!/bin/sh
# test_optimizer.py checks fixtures in a spawned process pool, which per-test coverage
# cannot see. Re-run it with a thread pool (map-building copy only) into .coverage.opt.
set -e
pip install -q -r requirements.lock pytest-cov coverage 2>&1 | grep -v notice || true
cd work/sqlglot
cp tests/test_optimizer.py /tmp/orig_test_optimizer.py
sed -i 's/ProcessPoolExecutor(mp_context=get_context("spawn"))/__import__("concurrent.futures").futures.ThreadPoolExecutor(max_workers=1)/' tests/test_optimizer.py
grep -n "ThreadPoolExecutor" tests/test_optimizer.py
COVERAGE_FILE=.coverage.opt SKIP_INTEGRATION=0 PYTHONPATH=. python -m pytest -q -p no:cacheprovider -n 4 \
  --cov=sqlglot --cov-context=test --cov-report= tests/test_optimizer.py > ../pytest_opt.log 2>&1 || true
cp /tmp/orig_test_optimizer.py tests/test_optimizer.py
tail -2 ../pytest_opt.log
