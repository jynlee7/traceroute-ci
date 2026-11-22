"""Dev only: writes sample-style synthetic commits for local evaluation.

    python3 tools/mutate.py OUT_DIR N [SEED]

Makes OUT_DIR/commits/<id>.patch (single-line operator flips, literal changes, fixture token
edits, added comments, multi-file mixes) against work/sqlglot. tools/label.sh then runs the full
suite on each one to find which tests really fail. Nothing here ships in the image.
"""

import ast
import json
import difflib
import io
import random
import re
import string
import sys
import tokenize
from pathlib import Path

ROOT = Path("work/sqlglot")

SWAPS = [
    (r"\band\b", "or"), (r"\bor\b", "and"),
    (r" is not ", " is "), (r" is not ", " != "), (r" is (?!not)", " == "), (r" is (?!not)", " is not "),
    (r" == ", " != "), (r" != ", " == "),
    (r" < ", " <= "), (r" <= ", " < "), (r" > ", " >= "), (r" >= ", " > "),
    (r"\bTrue\b", "False"), (r"\bFalse\b", "True"),
    (r"\bif not ", "if "), (r"\bnot (?=[a-z_(])", ""),
    (r"\bin\b(?= )", "not in"),
]


def code_lines(path: Path) -> list[int]:
    src = path.read_text()
    skip = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.STRING and tok.start[0] != tok.end[0]:
                skip.update(range(tok.start[0], tok.end[0] + 1))
    except tokenize.TokenError:
        return []
    out = []
    for i, line in enumerate(src.splitlines(), 1):
        s = line.strip()
        if i in skip or not s or s.startswith(("#", "import ", "from ", "@")):
            continue
        out.append(i)
    return out


def mutate_line(line: str, rng: random.Random) -> str | None:
    code, hash_, comment = line.partition("  #")
    options = []
    for pat, rep in SWAPS:
        for m in re.finditer(pat, code):
            options.append((m.start(), m.end(), rep))
    for m in re.finditer(r"(?<![\w.])(\d+)(?![\w.])", code):
        # Skip numbers inside string literals (rough: an odd number of quotes before).
        before = code[: m.start()]
        if before.count('"') % 2 or before.count("'") % 2:
            continue
        n = int(m.group(1))
        options.append((m.start(), m.end(), str(n + rng.choice([1, -1]) if n else 1)))
    if not options:
        return None
    a, b, rep = rng.choice(options)
    new = code[:a] + rep + code[b:]
    return new + hash_ + comment if hash_ else new


def diff(rel: str, old: str, new: str) -> str:
    body = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}", n=3))
    return f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n" + body


def code_mutant(rng: random.Random, files: list[Path]) -> tuple[str, str, str] | None:
    path = rng.choice(files)
    lines = code_lines(path)
    if not lines:
        return None
    src = path.read_text()
    rows = src.splitlines(True)
    for _ in range(20):
        i = rng.choice(lines)
        new_line = mutate_line(rows[i - 1].rstrip("\n"), rng)
        if new_line is None or new_line == rows[i - 1].rstrip("\n"):
            continue
        rows2 = rows[:]
        rows2[i - 1] = new_line + "\n"
        new = "".join(rows2)
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        rel = str(path.relative_to(ROOT))
        return rel, src, new
    return None


COMMENTS = ["# Kept for backwards compatibility", "# Intentionally not cached", "# Cheap check first",
            "# NOTE: order matters here", "# TODO: revisit", "# See the docstring above"]


def comment_mutant(rng: random.Random, files: list[Path]) -> tuple[str, str, str] | None:
    path = rng.choice(files)
    lines = code_lines(path)
    if not lines:
        return None
    src = path.read_text()
    rows = src.splitlines(True)
    i = rng.choice(lines)
    indent = re.match(r"\s*", rows[i - 1]).group(0)
    rows.insert(i - 1, indent + rng.choice(COMMENTS) + "\n")
    return str(path.relative_to(ROOT)), src, "".join(rows)


def fixture_mutant(rng: random.Random, fixtures: list[Path]) -> tuple[str, str, str] | None:
    path = rng.choice(fixtures)
    src = path.read_text()
    rows = src.splitlines(True)
    cands = [i for i, r in enumerate(rows) if r.strip() and not r.lstrip().startswith("--")]
    if not cands:
        return None
    i = rng.choice(cands)
    words = rows[i].rstrip("\n").split(" ")
    j = rng.randrange(len(words))
    op = rng.choice(["insert", "swap", "drop"])
    if op == "insert":
        words.insert(j, rng.choice(["x", "1", "AS", "b"]))
    elif op == "swap" and len(words[j]) > 1:
        w = words[j]
        k = rng.randrange(len(w) - 1)
        words[j] = w[:k] + w[k + 1] + w[k] + w[k + 2:]
    elif len(words) > 3:
        del words[j]
    else:
        words.insert(j, "x")
    rows[i] = " ".join(words) + "\n"
    new = "".join(rows)
    if new == src:
        return None
    return str(path.relative_to(ROOT)), src, new


def main() -> None:
    out, n = Path(sys.argv[1]), int(sys.argv[2])
    rng = random.Random(int(sys.argv[3]) if len(sys.argv) > 3 else 1)
    files = [p for p in (ROOT / "sqlglot").rglob("*.py") if p.stat().st_size > 2000]
    fixtures = sorted((ROOT / "tests/fixtures/optimizer").rglob("*.sql"))
    (out / "commits").mkdir(parents=True, exist_ok=True)
    made = 0
    while made < n:
        kind = rng.choices(["code", "fixture", "comment", "multi"], [55, 25, 10, 10])[0]
        parts = []
        if kind == "multi":
            for k in rng.sample(["code", "fixture", "comment", "comment"], rng.choice([2, 3])):
                parts.append(k)
        else:
            parts = [kind]
        edits, used = [], set()
        for k in parts:
            m = {"code": code_mutant, "comment": comment_mutant}.get(k)
            r = m(rng, files) if m else fixture_mutant(rng, fixtures)
            if r and r[0] not in used:
                used.add(r[0])
                edits.append(r)
        if len(edits) != len(parts):
            continue
        cid = "".join(rng.choices(string.ascii_lowercase + string.digits, k=4))
        patch = "".join(diff(*e) for e in edits)
        (out / "commits" / f"{cid}.patch").write_text(patch)
        (out / "edits").mkdir(exist_ok=True)
        (out / "edits" / f"{cid}.json").write_text(json.dumps({e[0]: e[2] for e in edits}))
        (out / "kinds.txt").open("a").write(f"{cid} {kind}\n")
        made += 1
    print(f"wrote {made} commits to {out}/commits")


if __name__ == "__main__":
    main()
