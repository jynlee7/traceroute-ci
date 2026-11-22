#!/bin/sh
# Fast local loop: run the selector on the host against the sample and score it.
cd "$(dirname "$0")/.." && DATA=sample SRC=work/sqlglot MAP=maps/impact.json.gz \
  python3 selector.py > work/out.jsonl 2> work/why.log && python3 score.py sample sample/answers.json work/out.jsonl
