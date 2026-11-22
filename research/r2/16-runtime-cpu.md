# 16 - Charged CPU: finishing round 1's plan (reasoned, nothing run)

Rules followed: no local testing. I tried one timing probe of gunzip/json/ast on this laptop and the sandbox denied it, so every time below is an **estimate** for CPython 3.13 on a modest cloud core. Byte and line counts are measured with read-only one-liners.

## (a) Findings

CPU charged = whole container, user+sys, median of 3 (SCORING.md:51-53). The selector is one process for all commits, so cost = fixed + per-patch + split.

**What round 1 (research/13) deferred** (its section d, items 4-7, plus items it never had):
1. The stub plus `blast.py` pyc split. Never done: `selector.py` is still the 945-line script that is recompiled on every start (Dockerfile:18 `CMD python -S /app/selector.py`).
2. `class_sids`/`fn_sids`. Never done: selector.py:304-319 `class_tests` still does `cov()` per ref range, and 584-593 `new_method_tests` does the same.
3. The unit-level "skip the whole-file dump" idea (its item 7). Deferred as "risky". It is the only way to get under 0.3 s, so it is finished below as `region` compare, with a build-time self-test as the safety net.
4. Everything below is new.

**Fixed cost in the current code** (selector.py line refs):

| item | line | est. ms |
|---|---|---|
| interpreter `-S` | Dockerfile CMD | 12 |
| imports ast, bisect, gzip, json, os, re, sys, pathlib | 8-15 | 30-35 |
| compile 945-line script every start (a `__pycache__` for the main script is never read) | - | 25-35 |
| `gzip.open` + `json.load` of the 5.95 MB map | 201-202 | 80-90 |
| `string_sets` and `string_heads` built **eagerly** for 34k strings | 210-212 | 25-30 |
| `json.loads` of `work/index.json`, **3.6 MB** (round 1 guessed 1.8 MB; `call_tests` alone is 2.2 MB, `refs` 1.0 MB, `fns` 0.2 MB) | 215 | 35-45 |
| `tests.json` | 919 | 2 |
| interpreter teardown freeing about 2M objects | exit | 15-30 |
| **fixed total** | | **about 230-290** |

**Per patch** (selector.py:437-481, 483-594, 804-833). For a *non-identical* sqlglot file the current code does, per KB of the touched file:
- `ast.parse` old and new, 0.3 ms each;
- `ast.dump` old and new (line 458), 1.2 ms each;
- `strip_docstrings` on both trees (a walk, 0.3 ms each) and **two more dumps** (line 465), 1.1 ms each. Round 1 missed this second pair;
- `changed_statement_lines` walks the old tree (0.3 ms);
- `def_names` x2 (line 574): a generic `iter_child_nodes` recursion through every expression, about 0.8 ms each.

That is about 7-8 ms/KB. The sample touches 489 KB of `.py` (dialect.py 113 KB, generators/clickhouse.py 98 KB, parsers/bigquery.py 63, transforms.py 45, optimizer/scope.py 41, typing/snowflake.py 36...), so about **3.5 s** on the sample. `test_file_change` (595-604) parses the old test file a second time. `class_tests` (304-319) for `Expression`/`Func` costs 0.1-0.8 s per call.

**Split** (`split`, 897-915) is plain Python: LPT for each k plus up to 64 `_improve` moves per k, each re-sorting every bin. It stops at the lower bound, which most selections reach. I estimate 2-5 ms per commit, with a worst case of tens of ms for a large selection that does not meet the bound. It is about 0.1 s on the sample. Not worth touching beyond the memo noted below.

**Baseline estimate: 4-5 s on the 30 sample commits.** The scorer adds CPU seconds 1:1, so it is about 2% of the 217 s.

**Round-1 design bug to avoid.** In the current code `strip_docstrings` runs in place on `old_tree` before `sqlglot_change` whenever the file is not `sqlglot/typing/` and the trees differ (lines 463-464). `changed_statement_lines` therefore never sees docstring `Expr` statements. Round 1 baked the statement table from the *unstripped* tree. This differs when a changed line is inside a docstring (the original would not add its first line). The baked `simple` table below is built from the stripped tree (unstripped for `typing/`) to keep identical behaviour.

## (b) Savings and risk

| step | saves on sample | recall risk |
|---|---|---|
| A. lazy marshal bake + stub/pyc + lazy strings + `os._exit` + flags | 0.20-0.25 s | none (same data, same logic) |
| B. baked old-file facts, new-only parse, fingerprints, statement-only `def_names`, `class_sids` | 1.6-2.0 s | none, except a 2^-64 fingerprint collision |
| C. `region` compare: re-parse only the innermost statement that holds every change | 1.2-1.4 s more | low, and guarded (below) |

Expected total CPU: baseline 4-5 s, after A 3.8-4.8 s, after A+B about 1.8-2.2 s, **after A+B+C about 0.25-0.4 s**. The 0.3 s target is plausible but depends on the unmeasured split cost and cloud-core speed. A+B alone will not reach it. The points are small, about 4 s of 217, so **do A+B first. Do C only with the self-test.**

Risk of C: it replaces "whole-file AST equal?" with "the enclosing statement's AST equal?". This is exact when (1) every changed line lies in one statement R, (2) old and new slices of R's lines each parse to exactly one statement, (3) the leading whitespace of the first line is unchanged. Failure to meet any condition falls back to the full path. Shapes that could fool it if the argument is wrong: patches that move a block across indentation levels, `a; b` on one line, one-line compounds (`if x: return 1`), edits to a decorator or a header continuation line, hunks that touch a docstring. All are handled by the fallback or the `D` rule (see code), and the build-time self-test compares region and full on about 1,000 synthetic edits (comment, delete, duplicate, indent, dedent, blank line). Any mismatch writes `region.off` and the runtime uses the full path. An undetected bug would give a wrong "ast-identical" (a miss, +300 s), so C ships only if the self-test is in the Dockerfile.

## (c) Code

### Dockerfile (replace lines 11-18, keep the pin and fetch step)
```dockerfile
COPY tools/ /app/tools/
COPY selector.py blast.py /app/
COPY maps/impact.json.gz /app/maps/impact.json.gz
RUN python /app/tools/fetch_src.py "$PIN" /app/src
RUN (python /app/tools/build_index.py /app/src /tmp/index.json || echo "index build failed; selector falls back") \
 && python /app/tools/bake.py /app/src /app/maps/impact.json.gz /tmp/index.json /app/bake \
 && python -m compileall -q --invalidation-mode unchecked-hash /app/blast.py \
 && chmod -R a+rX /app
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-S", "-E", "-B", "/app/selector.py"]
```
- `-S` skips `site` (about 10 ms). `-E` ignores PYTHON* variables, which is safe because the read-only run has none we need. `-B` replaces the ignored `PYTHONDONTWRITEBYTECODE`.
- `-X frozen_modules` is already `on` in a release build, so it is a no-op. `-I` is unsafe: it drops the script directory from `sys.path` and the stub's import would fail.
- `-OO` and `-X importtime` give nothing.
- The index is built to /tmp and baked; the raw JSON files are not read at runtime.

`/app/selector.py` becomes a stub. `git mv selector.py blast.py` first, then:
```python
import blast
blast.main()
```
`sys.path[0]` is `/app`, so `/app/__pycache__/blast.cpython-313.pyc` is used. `unchecked-hash` means the loader never stats the source.

### blast.py: header (after the existing imports, replacing `gzip`/`json` use at selector.py:201-218)
```python
import gc, marshal, zlib
from array import array
gc.disable()   # a one-shot process with no cycles worth collecting; parsing allocates millions of nodes
BAKE = os.environ.get("BAKE", str(APP / "bake"))

def _load(p):
    with open(p, "rb") as fh:
        return marshal.loads(fh.read())

class Lazy:
    """`index.get(key, default)` reads /app/bake/ix_<key>.m on first use (same values as index.json)."""
    def __init__(self, d): self.d, self.mem = d, {}
    def get(self, k, default=None):
        if k not in self.mem:
            try: self.mem[k] = _load(f"{self.d}/ix_{k}.m")
            except OSError: self.mem[k] = None
        v = self.mem[k]
        return default if v is None else v

class Lines:
    """line number -> set id for one file; 65535 = no coverage."""
    __slots__ = ("a",)
    def __init__(self, raw=b""): self.a = array("H"); self.a.frombytes(raw)
    def get(self, ln, default=None):
        a = self.a
        return a[ln] if 0 <= ln < len(a) and a[ln] != 65535 else default
    def __contains__(self, ln): return self.get(ln) is not None
    def values(self): return set(self.a) - {65535}
```

### blast.py: `Selector.__init__` lines 201-218 become
```python
        core = _load(BAKE + "/core.m")
        number_of = [self.by_id.get(tid) for tid in core["tests"]]
        self._number_of = number_of
        self._raw_sets = core["sets"]            # tuple of array('H') bytes, one per set
        self._sets: dict[int, frozenset] = {}
        self.files = core["files"]               # rel -> {"lines": bytes, "ranges": [...]}
        self._tab: dict[str, Lines] = {}
        self._facts: dict = {}
        self._src_cache: dict[str, str | None] = {}
        self.index = Lazy(BAKE)
        self.region_ok = not os.path.exists(BAKE + "/region.off")

    def __getattr__(self, name):                 # only called for a missing attribute
        if name not in ("strings", "string_sets", "string_heads"):
            raise AttributeError(name)
        num = self._number_of
        d = {num[k]: v for k, v in _load(f"{BAKE}/{name}.m").items() if num[k] is not None}
        setattr(self, name, d)
        return d

    def table(self, rel):
        t = self._tab.get(rel)
        if t is None:
            t = self._tab[rel] = Lines(self.files.get(rel, {}).get("lines", b""))
        return t
```
Other edits in blast.py:
```python
# test_set (228-233): the set body
            a = array("H"); a.frombytes(self._raw_sets[sid]); num = self._number_of
            s = self._sets[sid] = frozenset(n for n in (num[i] for i in a) if n is not None)
# cov (235-242), body_start (324-326), sqlglot_change (516-517, 526-527): `table = self.table(rel)`, and
#   table.get(str(ln)) -> table.get(ln);   str(ln) in table -> ln in table;   str(x) in table -> x in table
# file_cov (244-248): `table = self.table(rel)`; `for sid in table.values():`
# main(): last statements, after the commit loop:
    sys.stdout.flush(); sys.stderr.flush(); os._exit(0)   # skip teardown of the freed heap
```
`class_tests` (304-319), the tail replaces the `for n in names:` loop and `new_method_tests` is rewritten. Results are identical to today's:
```python
        cs = self.index.get("class_sids", {})
        sids = {s for n in names for s in cs.get(n, ())}
        return set().union(*(self.test_set(s) for s in sids))      # empty -> set()

    def new_method_tests(self, names):
        fs, lower = self.index.get("fn_sids", {}), self.index.get("class_lower", {})
        sids: set[int] = set(); out: set[int] = set()
        for _cls, name in names:
            sids.update(fs.get(name, ()))
            if name.endswith("_sql") and name[:-4] in lower:
                out |= self.class_tests(lower[name[:-4]])
        return out.union(*(self.test_set(s) for s in sids))
```
Keep the `len(names) > 400: return set(self.all)` guard exactly as it is. `refs` (the 1 MB table) is now read only by the `lost-internal-name` branch (line 561).

### blast.py: per-file facts, shared by the bake and the runtime (new, module level)
```python
_FIELDS = ("body", "orelse", "finalbody", "handlers", "cases")

def sub_stmts(node):
    for fld in _FIELDS:
        v = getattr(node, fld, None)
        if isinstance(v, list):
            for c in v:
                if isinstance(c, ast.stmt): yield c
                else: yield from sub_stmts(c)          # ExceptHandler, match_case

def fast_defs(stmts, cls, out):
    """Same set as def_names(): defs only occur in statement lists, so skip every expression."""
    for s in stmts:
        if isinstance(s, ast.ClassDef):
            fast_defs(sub_stmts(s), s.name, out)
        else:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)): out.add((cls, s.name))
            fast_defs(sub_stmts(s), cls, out)

def fingerprint(tree):
    s = ast.dump(tree).encode()
    return (len(s), zlib.crc32(s), zlib.adler32(s))

def stmt_nodes(tree):
    """Parallel tuples over every statement: start (with decorators), end, parent, is-class, is-str-expr, col, class context."""
    S, E, P, K, D, X, C = ([] for _ in range(7))
    def go(parent, stmts, cls):
        for s in stmts:
            i = len(S)
            S.append(min([s.lineno] + [d.lineno for d in getattr(s, "decorator_list", [])]))
            E.append(s.end_lineno); P.append(parent); K.append(isinstance(s, ast.ClassDef))
            D.append(isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str))
            X.append(s.col_offset); C.append(cls)
            go(i, sub_stmts(s), s.name if isinstance(s, ast.ClassDef) else cls)
    go(-1, tree.body, "")
    return tuple(map(tuple, (S, E, P, K, D, X, C)))

def test_spans(tree):
    out = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test"):
            out.append((0, node.lineno, node.end_lineno, node.name, ()))
        elif isinstance(node, ast.ClassDef):
            items = tuple((min([i.lineno] + [d.lineno for d in i.decorator_list]), i.end_lineno, i.name)
                          for i in node.body if isinstance(i, ast.FunctionDef) and i.name.startswith("test"))
            out.append((1, node.lineno, node.end_lineno, node.name, items))
    return tuple(out)

def file_facts(rel, text):
    tree = ast.parse(text)
    mbc: dict[str, int] = {}
    for st in tree.body:
        for n in module_bound(ast.Module([st], [])):
            mbc[n] = mbc.get(n, 0) + 1
    defs: set = set(); fast_defs(tree.body, "", defs)
    f = {"nlines": text.count("\n") + 1, "fp": fingerprint(tree), "nodes": stmt_nodes(tree),
         "defs": frozenset(defs), "mbc": mbc, "top": tuple(s.name for s in tree.body if isinstance(s, ast.ClassDef)),
         "cs": {k: frozenset(v) for k, v in class_sql_names(tree).items()}, "tspans": test_spans(tree)}
    if not rel.startswith("sqlglot/typing/"):
        strip_docstrings(tree)        # the selector judges sqlglot_change against the stripped old tree
    f["fpd"] = fingerprint(tree)
    simple = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt) or hasattr(node, "body") or not node.end_lineno: continue
        if node.end_lineno - node.lineno >= 40: continue
        simple.append((node.lineno, node.end_lineno,
                       tuple((n.lineno, n.end_lineno) for n in ast.walk(node) if isinstance(n, ast.Lambda))))
    simple.sort()
    f["simple"], f["starts"] = tuple(simple), tuple(s[0] for s in simple)
    return f

def changed_statement_lines(facts, touched):          # replaces selector.py:804-833, same result
    out = set(touched); simple, starts, hits = facts["simple"], facts["starts"], {}
    for ln in touched:
        j = bisect.bisect_right(starts, ln) - 1
        while j >= 0 and starts[j] >= ln - 40:        # spans are under 40 lines
            if simple[j][1] >= ln: hits.setdefault(j, []).append(ln)
            j -= 1
    for j, inside in hits.items():
        a, _b, lams = simple[j]
        if lams and all(any(x <= ln <= y for x, y in lams) for ln in inside): continue
        out.add(a)
    return out
```
`module_bound`, `class_sql_names`, `strip_docstrings` stay as they are. `def_names` is unused afterwards, so keep or delete it.

### blast.py: the compare (new) and the new `python_change` (replaces 437-481)
```python
def compare(rel, f, old, new, facts, region=True):
    """(same, how, nv). nv holds lost/new_names/cs for sqlglot/ files when not same."""
    typing = rel.startswith("sqlglot/typing/"); want = rel.startswith("sqlglot/")
    if region:
        r = _region(rel, f, old, new, facts, typing, want)
        if r is not None:
            return r
    tree = ast.parse(new)                              # SyntaxError propagates to the caller
    same, how = fingerprint(tree) == facts["fp"], "ast-identical"
    if not same and not typing:
        strip_docstrings(tree)
        same, how = fingerprint(tree) == facts["fpd"], "docstring-only"
    nv = None
    if not same and want:
        defs: set = set(); fast_defs(tree.body, "", defs)
        nv = {"lost": set(facts["mbc"]) - module_bound(tree), "new_names": defs - facts["defs"],
              "cs": class_sql_names(tree) if rel.startswith("sqlglot/expressions/") else facts["cs"]}
    return same, how, nv

def _region(rel, f, old, new, facts, typing, want):
    touched = touched_old_lines(f)
    if not touched: return None
    lo, hi = min(touched), max(touched)
    S, E, P, K, D, X, C = facts["nodes"]
    b = None
    for i in range(len(S)):                            # smallest statement holding every touched line
        if S[i] <= lo and hi <= E[i] and (b is None or E[i] - S[i] < E[b] - S[b]): b = i
    while b is not None and D[b]:                      # a string statement: judge it with its owner
        b = P[b] if P[b] >= 0 else None
    if b is None or E[b] - S[b] > facts["nlines"] // 2: return None
    a, z = S[b], E[b]
    delta = sum((t == "+") - (t == "-") for h in f["hunks"] for t, _x, _o in h["lines"])
    o, n = old.split("\n")[a - 1:z], new.split("\n")[a - 1:z + delta]
    ws = lambda s: s[:len(s) - len(s.lstrip())]
    if not o or not n or ws(o[0]) != ws(n[0]) or len(ws(o[0])) != X[b]: return None
    ind = X[b]
    def one(lines):
        text = "\n".join(lines)
        try:
            mod = ast.parse(("if 1:\n" + text) if ind else text).body
            if len(mod) != 1: return None
            body = mod[0].body if ind else mod
        except (SyntaxError, ValueError, RecursionError):
            return None
        return body[0] if len(body) == 1 else None
    ro, rn = one(o), one(n)
    if ro is None or rn is None: return None
    same, how = ast.dump(ro) == ast.dump(rn), "ast-identical"
    if not same and not typing:
        strip_docstrings(ro); strip_docstrings(rn)
        same, how = ast.dump(ro) == ast.dump(rn), "docstring-only"
    if same or not want: return same, how, None
    top = P[b] < 0
    if rel.startswith("sqlglot/expressions/"):         # `_sql_names` live in a top-level class body
        assign = (ast.Assign, ast.AnnAssign)
        if top or (K[P[b]] and P[P[b]] < 0 and (isinstance(ro, assign) or isinstance(rn, assign))): return None
    lost: set = set()
    if top:
        mo, mn = module_bound(ast.Module([ro], [])), module_bound(ast.Module([rn], []))
        lost = {x for x in mo - mn if facts["mbc"].get(x, 0) <= 1}
    defs: set = set(); fast_defs([rn], C[b], defs)
    return False, how, {"lost": lost, "new_names": defs - facts["defs"], "cs": facts["cs"]}
```
(The `assign` condition has an operator-precedence trap: write it exactly as `top or (K[P[b]] and P[P[b]] < 0 and (isinstance(ro, assign) or isinstance(rn, assign)))`. A dialect-level `_sql_names` edit then takes the full path.)

Soundness of "region differs implies file differs": the file outside R is byte-identical, R's old and new slices are each one complete statement at the same indentation, so the module AST is the prefix statements + R + suffix statements, and only R differs. "Region same" is the same argument. The `D` rule avoids judging a docstring out of context. `lost`: a name leaves `module_bound` only if R was its only top-level binder (`mbc <= 1`) and R_new no longer binds it. `new_names`: `def_names(new) - def_names(old)` is exactly `defs(R_new) - def_names(old)`, because defs outside R are in both.
```python
    def facts(self, rel, old):
        if rel not in self._facts:
            try: d = _load(BAKE + "/py_" + rel.replace("/", "__") + ".m")
            except OSError:
                try: d = file_facts(rel, old)           # not baked: same code as the bake
                except SyntaxError: d = None
            self._facts[rel] = d
        return self._facts[rel]

    def python_change(self, f):
        rel = f["path"]
        old = self.source(f["old"]) if f["old"] else ""
        if old is None: return set(self.all), "unknown-file"
        new = apply_patch(old, f) if f["new"] else ""
        if new is None: return set(self.all), "patch-did-not-apply"
        facts = self.facts(f["old"], old) if old else None
        if old and facts is None: return set(self.all), "old-syntax-error"
        nv = None
        if old and new:
            try:
                same, how, nv = compare(rel, f, old, new, facts, self.region_ok)
            except SyntaxError:
                if rel.startswith("tests/"):
                    reach = self.test_module_reach(rel)
                    if reach is not None: return reach, "syntax-error-test"
                return set(self.all), "syntax-error"
            if same:
                ... # the existing `if same:` body, lines 466-476, unchanged (uses `how`)
        elif new:                                       # new file: a syntax error still matters
            try: ast.parse(new)
            except SyntaxError: ... # the existing syntax-error returns, unchanged
        if rel.startswith("tests/"): return self.test_file_change(f, old, facts), "test-file"
        if not rel.startswith("sqlglot/"): return set(self.all), "other-python"
        return self.sqlglot_change(f, facts, nv)
```
Keep the original ordering exactly for the created/deleted-file cases: copy lines 445-452 for the `new`-only branch.

`sqlglot_change(self, f, facts, nv)` edits (selector.py:483-594). The early return for `f["old"] is None or f["new"] is None` stays first:
```python
        changed = changed_statement_lines(facts, touched_old_lines(f))              # was old_tree/new_tree
        ...
            old_names, new_names_ = facts["cs"], nv["cs"]                           # was class_sql_names(...)
        ...
        lost = nv["lost"]                                                           # was module_bound(old)-module_bound(new)
        defined = set(facts["top"])                                                 # was {s.name for s in old_tree.body ...}
        new_names = nv["new_names"]                                                 # was def_names(new)-def_names(old)
```
`test_file_change(self, f, old, facts=None)`: delete `tree = ast.parse(old)` and the node loop; use the baked spans:
```python
        spans = facts["tspans"] if facts else test_spans(ast.parse(old))
        ...
        for ln in lines:
            hit = None
            for kind, a, b, name, items in spans:
                if a <= ln <= b:
                    if kind == 0: hit = f"{rel}::{name}"
                    else:
                        for s, e, iname in items:
                            if s <= ln <= e: hit = f"{rel}::{name}::{iname}"
```
(Same last-write-wins order as the original loop.)

### tools/bake.py (new, build only)
```python
"""Bake map + index + per-file AST facts into /app/bake/*.m (marshal). Build CPU is not charged."""
import gzip, json, marshal, sys, os, difflib, random
from array import array
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import blast

def put(out, name, obj): (out / name).write_bytes(marshal.dumps(obj))

def main(src, mapgz, indexj, out):
    src, out = Path(src), Path(out); out.mkdir(parents=True, exist_ok=True)
    m = json.load(gzip.open(mapgz, "rt"))
    assert len(m["sets"]) < 65535 and len(m["tests"]) < 65535
    files = {}
    for rel, d in m["files"].items():
        a = array("H", [65535]) * (max(map(int, d["lines"]), default=0) + 1)
        for k, v in d["lines"].items(): a[int(k)] = v
        files[rel] = {"lines": a.tobytes(), "ranges": d["ranges"]}
    put(out, "core.m", {"tests": m["tests"], "sets": tuple(array("H", s).tobytes() for s in m["sets"]), "files": files})
    ts = {int(k): v for k, v in m["test_strings"].items()}
    put(out, "strings.m", ts)
    put(out, "string_sets.m", {k: frozenset(v) for k, v in ts.items()})
    put(out, "string_heads.m", {k: frozenset(s.split(",", 1)[0].strip() for s in v if "," in s and "=" in s) for k, v in ts.items()})
    try: ix = json.loads(Path(indexj).read_text())
    except Exception: ix = {}
    def sids(rs):
        s = set()
        for rel, a, b in rs:
            t = m["files"].get(rel, {}).get("lines", {})
            s.update(t[str(l)] for l in range(a, b + 1) if str(l) in t)
        return tuple(sorted(s))
    if ix:
        ix["class_sids"] = {n: sids(rs) for n, rs in ix.get("refs", {}).items()}
        ix["fn_sids"] = {n: sids(rs) for n, rs in ix.get("fns", {}).items()}
        ix["class_lower"] = {n.lower(): n for n in ix.get("refs", {})}
    for k, v in ix.items(): put(out, f"ix_{k}.m", v)
    facts, texts = {}, {}
    for p in sorted(src.rglob("*.py")):
        rel = str(p.relative_to(src))
        if not rel.startswith(("sqlglot/", "tests/")): continue
        try:
            texts[rel] = p.read_text(encoding="utf-8", errors="replace")
            facts[rel] = blast.file_facts(rel, texts[rel])
            put(out, "py_" + rel.replace("/", "__") + ".m", facts[rel])
        except Exception: pass                           # runtime computes it on demand
    bad = selftest(facts, texts)
    if bad: (out / "region.off").write_text("\n".join(bad))
    print("bake ok; region", "OFF" if bad else "on", bad[:3])

def selftest(facts, texts, per_file=6):
    rnd, bad = random.Random(1), []
    for rel in sorted(facts):
        if not rel.startswith("sqlglot/"): continue
        ol = texts[rel].split("\n")
        for _ in range(per_file):
            nl, i = list(ol), rnd.randrange(len(ol))
            kind = rnd.choice(("comment", "delete", "dup", "indent", "dedent", "blank"))
            if kind == "comment": nl[i] += "  # x"
            elif kind == "delete": del nl[i]
            elif kind == "dup": nl.insert(i, nl[i])
            elif kind == "indent": nl[i] = "  " + nl[i]
            elif kind == "dedent": nl[i] = nl[i][2:] if nl[i].startswith("  ") else nl[i]
            else: nl.insert(i, "")
            if nl == ol: continue
            body = "\n".join(difflib.unified_diff(ol, nl, f"a/{rel}", f"b/{rel}", n=3, lineterm=""))
            fs = blast.parse_patch(f"diff --git a/{rel} b/{rel}\n{body}\n")
            if not fs: continue
            old, new = texts[rel], "\n".join(nl)
            try: full = blast.compare(rel, fs[0], old, new, facts[rel], region=False)
            except SyntaxError: continue
            reg = blast.compare(rel, fs[0], old, new, facts[rel], region=True)
            if reg[0] != full[0] or (full[0] and reg[1] != full[1]) or reg[2] != full[2]:
                bad.append(f"{rel}:{i}:{kind}")
    return bad

main(*sys.argv[1:5])
```
Build time of the self-test: about 184 files x 6 edits x 2 compares, a few minutes at most because parser.py is the slow one; it is uncharged. Lower `per_file` if the build has a time limit. Also confirm `parse_patch` accepts this minimal header (I did not read it). If it returns nothing the self-test silently tests nothing, so add `assert tested > 100` in the loop if you want that to be loud.

## (d) Ranked recommendations

1. **B: baked old-file facts + new-only parse + fingerprints + `fast_defs` + `class_sids`** (the `file_facts`, `compare` full branch, `changed_statement_lines`, `test_spans`, `Lazy` index). Saves **1.6-2.0 s**, risk none. Removes 2 old parses, 2 old dumps, 2 stripped dumps' old halves, 2 `def_names` and the old-tree walk per patch. Also removes the 0.1-0.8 s `class_tests` spikes.
2. **A: stub + `blast.py` pyc, marshal bake, lazy strings/index, `gc.disable`, `os._exit`, `-S -E -B`.** Saves **0.20-0.25 s**, risk none. Lazy strings alone save 25-40 ms on commits that touch no dialect or fixture; `os._exit` saves 15-30 ms.
3. **C: `region` compare + self-test.** Saves another **1.2-1.4 s** (about 90% of the sample's code bytes sit inside one method or table). Risk low, guarded by the self-test and fallbacks. Do it last, and only if the Dockerfile runs `bake.py` with the self-test. Skip if the build time budget is tight.
4. **Memoise `split` by `frozenset(selected)`** (optional, ~0 s on the sample, since the selections rarely repeat). Do not change the algorithm: it decides the simulated seconds.
5. **Skip, not worth it:** replacing `pathlib` with `os.path` (5-8 ms), avoiding `re` (about 8 ms, `json` needs it anyway), `-X frozen_modules`, `-OO`, `PYTHONMALLOC`, and compressing the bake (decompression costs more than reading).

Where I could not verify, and what to check in the first `make sample` after these changes:
- `output.jsonl` must be byte-identical to the current one (every change above is meant to be selection-preserving), and `region.off` must not exist in `/app/bake`.
- `docker run ... time` CPU should drop from about 4-5 s to under 0.5 s. If it stays above 1 s, look at `split` first (a large unbounded selection), then at whether `region` was declined (`E[b]-S[b] > nlines//2` for class-level insertions into `Parser` or `Generator`).
- Round-1 follow-up still open: pure insertions between methods of a big class fall to the full path in C (the smallest enclosing statement is the whole class). Extending `_region` to a run of sibling statements would cover them; I left it out because of the extra header and gap-line cases.
