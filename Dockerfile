# The scorer builds this on linux/amd64 and runs it with no network.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# sqlglot at the pinned commit, straight from its GitHub archive (no apt, no git): the
# selector reads the original sources to apply each patch and compare syntax trees.
# No tests run at build time.
ARG PIN=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
COPY tools/fetch_src.py tools/build_index.py /app/tools/
RUN python /app/tools/fetch_src.py "$PIN" /app/src

COPY selector.py /app/selector.py
COPY maps /app/maps
# Static index of import-time code (class tables, dialect families); build CPU is not charged.
RUN (python /app/tools/build_index.py /app/src /app/maps/index.json || echo "index build failed; selector falls back") \
 && chmod -R a+rX /app

ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-S", "/app/selector.py"]
