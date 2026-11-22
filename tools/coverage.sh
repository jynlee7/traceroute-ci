#!/bin/sh
# Builds per-test coverage of sqlglot at the pinned commit. Run inside python:3.13-slim:
#   docker run --rm -v "$PWD":/w -w /w python:3.13-slim sh tools/coverage.sh
set -e
PIN=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
apt-get update -qq >/dev/null && apt-get install -y -qq git >/dev/null
mkdir -p work && cd work
if [ ! -d sqlglot ]; then
  git init -q sqlglot && cd sqlglot && git remote add origin https://github.com/tobymao/sqlglot.git
  git fetch -q --depth 1 origin $PIN && git checkout -q FETCH_HEAD && cd ..
fi
pip install -q -r ../requirements.lock pytest-cov coverage 2>&1 | tail -2
cd sqlglot
SKIP_INTEGRATION=0 PYTHONPATH=. python -m pytest -q -p no:cacheprovider -n 4 \
  --cov=sqlglot --cov-context=test --cov-report= tests > ../pytest.log 2>&1 || true
tail -3 ../pytest.log
ls -la .coverage*
