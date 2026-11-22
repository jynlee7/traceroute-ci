"""Dev only: runs the full suite on each synthetic commit and writes answers.json.

Run in an arm64 python:3.13-slim container with requirements.lock installed:
    docker run --rm -v "$PWD":/w -w /w br-label python tools/label.py work/synth
"""
import json, os, re, shutil, subprocess, sys
from pathlib import Path

out = Path(sys.argv[1])
src = Path("work/sqlglot")
tests = json.loads(Path("sample/tests.json").read_text())["tests"]
num = {t["id"]: t["number"] for t in tests}
by_file = {}
for t in tests:
    by_file.setdefault(t["file"], []).append(t["number"])
ans_path = out / "answers.json"
answers = json.loads(ans_path.read_text()) if ans_path.exists() else {"commits": {}}
kinds = dict(l.split() for l in (out / "kinds.txt").read_text().splitlines())
deselect = json.loads((out / "deselect.json").read_text()) if (out / "deselect.json").exists() else {}
IGNORE = {n for t, n in num.items() if t.startswith("tests/test_sqlglotc.py")}
line_re = re.compile(r"^(FAILED|ERROR|SUBFAILED\S*)\s+(\S+)")
order = (out / "order.txt").read_text().split() if (out / "order.txt").exists() else []
rank = {c: i for i, c in enumerate(order)}
for edit in sorted((out / "edits").glob("*.json"), key=lambda e: (rank.get(e.stem, 1e9), e.stem)):
    cid = edit.stem
    if cid in answers["commits"]:
        continue
    w = Path("/tmp/w")
    shutil.rmtree(w, ignore_errors=True)
    w.mkdir()
    for d in ("sqlglot", "tests"):
        shutil.copytree(src / d, w / d, ignore=shutil.ignore_patterns("__pycache__", "*.so"))
    for rel, text in json.loads(edit.read_text()).items():
        (w / rel).write_text(text)
    env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=str(w), PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-n", "8",
                        "--timeout", "900", "-rfE", "tests"] + [a for t in deselect.get(cid, []) for a in ("--deselect", t)], cwd=w, env=env, capture_output=True, text=True)
    failing = set()
    for line in r.stdout.splitlines():
        m = line_re.match(line)
        if not m:
            continue
        nid = m.group(2).split("[")[0]
        if nid in num:
            failing.add(num[nid])
        elif nid in by_file:  # collection error: the whole file fails
            failing.update(by_file[nid])
    failing -= IGNORE
    answers["commits"][cid] = {"failing": sorted(failing), "kind": kinds.get(cid, "?")}
    ans_path.write_text(json.dumps(answers))
    print(cid, kinds.get(cid), len(failing), r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-300:], flush=True)
