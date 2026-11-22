# The scorer builds this on linux/amd64 and runs it with no network.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# sqlglot at the pinned commit, straight from its GitHub archive (no apt, no git): the
# selector reads the original sources to apply each patch and compare syntax trees.
# No tests run at build time.
ARG PIN=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
COPY tools/fetch_src.py /app/tools/
RUN python /app/tools/fetch_src.py "$PIN" /app/src
# The labeling environment (requirements.lock is it, on this base image): the statement probe runs
# parts of sqlglot's tests in-process, which import duckdb, pandas and pytz.
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -q -r /app/requirements.lock

COPY tools/build_index.py tools/tpc_probe.py tools/stmt_probe.py /app/tools/
COPY selector.py /app/selector.py
COPY maps /app/maps
# Static index of import-time code (class tables, dialect families); build CPU is not charged.
# Then a marshal snapshot of map + index for the runtime (same interpreter; JSON is the fallback).
RUN (python /app/tools/build_index.py /app/src /app/maps/index.json || echo "index build failed; selector falls back") \
 && (python -S /app/selector.py --bake || echo "bake failed; selector reads JSON") \
 && chmod -R a+rX /app

# Fingerprints of what test_execute_tpcds computes with the pinned sources (parsed and optimized
# TPC-DS queries, their DuckDB SQL, every query's result): the selector's TPC probe compares a
# commit's patched sources against them. Written only on success; without it the probe is off.
RUN (PYTHONHASHSEED=0 python /app/tools/tpc_probe.py /app/src --baseline > /tmp/tpc_base.json \
     && mv /tmp/tpc_base.json /app/maps/tpc_base.json || echo "tpc baseline failed; probe off") \
 && chmod -R a+rX /app
# Per-query cold line coverage of the same three calls: the runtime probe recomputes only the
# queries an edit can reach and keeps the baseline fingerprint for the rest. Optional.
RUN (PYTHONHASHSEED=0 python /app/tools/tpc_probe.py /app/src --qcov > /tmp/tpc_qcov.json \
     && mv /tmp/tpc_qcov.json /app/maps/tpc_qcov.json || echo "tpc qcov failed; probe runs every query") \
 && chmod -R a+rX /app

# Bytecode for the sources the probes import (the disk is read-only at run time, so without it
# every probe would compile sqlglot again).
RUN python -m compileall -q -j 0 /app/src/sqlglot /app/src/tests /app/tools /app/selector.py && chmod -R a+rX /app

ENV PYTHONDONTWRITEBYTECODE=1
# Imported, not run as a script: a script is compiled on every start, a module loads its .pyc.
CMD ["python", "-S", "-c", "import sys; sys.path.insert(0, '/app'); import selector; selector.entry()"]
