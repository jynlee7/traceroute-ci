# 13 - Runtime CPU of selector.py (reasoned, nothing run)

Scoring counts CPU of the whole container run once (SCORING.md:51-53: all processes, user+sys, median of 3 runs). The selector handles all commits in one process (`selector.py:main`). So this is one fixed cost plus a sum over patches. Build CPU is free.

All times below are estimates for CPython 3.13 on a modest cloud core. I did not measure them. Data sizes are measured with read-only one-liners.

## (a) Findings

Measured sizes:
- `maps/impact.json.gz`: 770,871 bytes gzipped, **5,952,978 bytes** as JSON.
  - `sets`: 5.0 MB. 8,966 sets, 930,704 int members.
  - `files`: 745 KB. 184 files, 53,696 `lines` entries, 4,859 ranges. The keys are strings.
  - `test_strings`: 1.16 MB. 1,620 tests, 33,975 strings.
  - `tests`: 109 KB.
- `work/sqlglot/sqlglot/**/*.py`: 184 files, 88,223 lines, 3.17 MB. `parser.py` is 423,632 bytes and 11,017 lines. `expressions/core.py` is 93 KB.
- `index.json` has no size on disk yet, so I estimated it. A regex pass found 1,250 class names and **25,623 (class, line) ref entries**, about 1.2 MB of `refs` as JSON (`["sqlglot/parser.py",123,123]` each). Add `fns`, `toplevel`, `lambdas`, `rdeps` and `subs` (a few hundred KB) for about 1.6-2 MB in total.
- The most-referenced classes are `TokenType` (2,744 refs), `DType` (1,449), `Expr` (1,333), `Expression` (925), `Literal` (629) and `Func` (541).

Fixed cost per run (estimated):

| item | where | est. |
|---|---|---|
| interpreter start with `-S` | Dockerfile:28 | 10-15 ms |
| imports (ast, gzip, json, re, pathlib) | selector.py:9-15 | 25-35 ms |
| compiling `selector.py` (~900 lines) on every start | see next paragraph | 15-25 ms |
| `gzip` decompress 6 MB | selector.py:152 | 20-25 ms |
| `json.load` of 6 MB (930k ints, 54k str keys) | selector.py:152 | 50-70 ms |
| `strings` and `string_sets` (34k strings into 1,620 frozensets) | selector.py:160-161 | 10-15 ms |
| `json.loads` of index.json (~1.8 MB) | selector.py:165 | 15-20 ms |
| total fixed | | about 170-220 ms |

**Wasted bytecode compile.** The Dockerfile runs `compileall` on `selector.py` (Dockerfile:27). A script run as `python /app/selector.py` is **never loaded from a `.pyc`**. The `__pycache__` file it creates is unused, so the main script is recompiled on every run. Only imported modules use the cache. `PYTHONDONTWRITEBYTECODE` stops writes, not reads.

Per-patch cost (estimated, `python_change` at selector.py:276-297). Per KB of a touched `.py` file:
- `ast.parse(new)` is about 0.3 ms/KB.
- `ast.parse(old)` is about 0.3 ms/KB.
- `ast.dump(old)` and `ast.dump(new)` (selector.py:292) are pure Python recursion and cost about 1.2 ms/KB each. They are the largest term.
- `ast.walk(old_tree)` in `changed_statement_lines` (selector.py:~461) costs about 0.3 ms/KB. It runs only if the trees differ.
- `def_names` on both trees (selector.py:~323) is about 0.4 ms/KB each.
- `test_file_change` re-parses `old` (selector.py:~348 `tree = ast.parse(old)`), although `python_change` already parsed it.

That sums to about 4 ms per KB of touched source. A patch to `parser.py` (424 KB) costs about 1.7 s of CPU. The 30 sample patches touch about 500 KB of `.py` source, so about 2 s in total. Hidden commits that touch `parser.py`, `generator.py` or `dialect.py` (116 KB) each cost 0.5-1.7 s.

`class_tests` (selector.py:~226) is called from `import_time_change` and `new_method_tests`.
- It calls `self.cov(rel, range(start, end+1))` once per ref entry. Each call builds `str(ln)` keys and does `out |= frozenset`.
- For `Expression`, `Expr` or `Func`, `subs` gives up to 400 names (the loop cap). That is up to about 20k ref entries, each doing a union with a frozenset of up to 1,620 members.
- Cost is 20-30k lookups plus up to 20k set unions of 100-1,600 members, about **0.2-0.8 s per call**.
- It is also first-use `test_set` construction for up to 8,966 sets (930k members, about 80 ms total if all are touched).
- Nothing is memoized across files or patches.
- `new_method_tests` calls `class_tests` for every new `*_sql` def. It runs again for each patch that adds one.

Estimated total on the 30 sample patches: 0.2 (fixed) + 2 (parse/dump) + 0.5-2 (class_tests) = **about 3-4 s**, roughly 2% of the 192 s score. With about 0.1 s per commit it is a modest term, but cheap to cut. The target is about 1.0-1.3 s.

## (b) Risks and waste

Recall risk: none from these changes if they are done as below, because every precomputed value is built from the pinned source and the coverage map. These caveats do apply:
1. The marshal bake is Python-version-specific. Build and run use the same image, so it is fine. The bake must be built with the same interpreter (`RUN python ...`).
2. The trimmed `class_sids` / `fn_sids` are exactly the sids that `cov()` over the old `refs` / `fns` ranges would give. The result is identical to today's, not an approximation.
3. The fingerprint comparison replaces `ast.dump(a) == ast.dump(b)` with `(len, crc32, adler32)` of the dump text. A false "identical" would drop tests. The chance is about 2^-64 per comparison, so negligible.
4. Do not truncate `refs` to the "lines with coverage" at build time by dropping lines. Coverage lives in the map and the index builder does not read it, so the trim must happen in the bake step, which sees both.

Waste, in order of size:
- `ast.dump` x2 for each touched file.
- Old-file parse, walk and `def_names`. They are pure functions of the pinned source and can be baked.
- `class_tests` doing per-line work. All of it can be precomputed to `class -> set of set-ids`.
- JSON of 6 MB, where marshal is 3-5x faster and avoids `str(ln)` lookups.
- Recompiling the main script.

## (c) Code changes

### Design
Add `tools/bake.py` (build time, uncharged). It reads `maps/impact.json.gz`, `maps/index.json` and the sources, and writes `maps/bake.bin`, one marshal blob. `build_index.py` needs **no change**: its `refs` map has every class name as a key, because of the `setdefault` at build_index.py:~120, and the bake trims it. Turn `selector.py` into a 3-line stub that imports `blast.py`, which is the old selector with the edits below. `blast.py` is an imported module, so its `.pyc` is used.

### Dockerfile (replace lines 22-28)
```dockerfile
COPY blast.py selector.py /app/
COPY tools/build_index.py tools/bake.py /app/tools/
COPY maps/impact.json.gz /app/maps/impact.json.gz
# Static index + baked marshal blob (build CPU is not charged).
RUN python /app/tools/build_index.py /app/src /app/maps/index.json \
 && python /app/tools/bake.py /app/src /app/maps/impact.json.gz /app/maps/index.json /app/maps/bake.bin \
 && python -m compileall -q --invalidation-mode unchecked-hash /app/blast.py \
 && rm /app/maps/index.json /app/maps/impact.json.gz \
 && chmod -R a+rX /app
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-S", "/app/selector.py"]
```
`selector.py` becomes:
```python
from blast import main
main()
```
`sys.path[0]` is `/app` even with `-S`, so `import blast` resolves and reads `/app/__pycache__/blast.cpython-313.pyc`. Keep `.pyc` mtime irrelevant by using `unchecked-hash`. Do not delete the sources the selector still reads (`/app/src`).

### tools/bake.py (new)
```python
"""Build-time bake: map + index + per-file static facts -> one marshal blob. Not charged."""
import ast, gzip, json, marshal, sys, zlib
from pathlib import Path

sys.path.insert(0, "/app")  # for blast.file_facts / blast.fingerprint (same code as runtime)
from blast import file_facts

def main():
    src, mapgz, indexj, out = (Path(a) for a in sys.argv[1:5])
    m = json.load(gzip.open(mapgz, "rt"))
    idx = json.loads(indexj.read_text())
    files = {rel: ({int(k): v for k, v in d["lines"].items()},
                   tuple(tuple(r) for r in d["ranges"])) for rel, d in m["files"].items()}

    def sids(rel, a, b):
        t = files[rel][0] if rel in files else {}
        return {t[l] for l in range(a, b + 1) if l in t}

    refs, fns = idx.get("refs", {}), idx.get("fns", {})
    class_sids = {}
    for n, rs in refs.items():
        s = set()
        for rel, a, b in rs:
            s |= sids(rel, a, b)
        if s:
            class_sids[n] = frozenset(s)
    fn_sids = {}
    for n, rs in fns.items():
        s = set()
        for rel, a, b in rs:
            s |= sids(rel, a, b)
        if s:
            fn_sids[n] = frozenset(s)
    lean = {k: idx.get(k, {}) for k in ("toplevel", "lambdas", "subs", "rdeps")}
    lean["class_sids"], lean["fn_sids"] = class_sids, fn_sids
    lean["class_lower"] = {n.lower(): n for n in refs}  # new_method_tests keys on every class name

    perfile = {}
    for p in sorted((src / "sqlglot").rglob("*.py")):
        rel = str(p.relative_to(src))
        try:
            perfile[rel] = marshal.dumps(file_facts(ast.parse(p.read_text(encoding="utf-8"))))
        except Exception:
            pass  # runtime falls back to parsing this file

    blob = {"tests": m["tests"], "sets": [tuple(s) for s in m["sets"]], "files": files,
            "strings": {int(k): frozenset(v) for k, v in m["test_strings"].items()},
            "index": lean, "perfile": perfile}
    out.write_bytes(marshal.dumps(blob))

main()
```
The `refs` -> `class_sids` trim is the "refs index trimmed to lines with coverage" idea, taken one step further. At runtime `refs` is not loaded at all (1.2 MB, 25k entries). If this step crashes the build must still work: `bake.py` is under `&&`. If you prefer never-fail, append `|| true` and keep a JSON fallback in `load_maps` (below).

### blast.py (the old selector.py plus these edits)

1. Imports and GC. Drop `gzip` and `json` from the hot path and add:
```python
import bisect, gc, marshal, zlib
gc.disable()   # ast.parse / json build millions of objects; no cycles to collect in a 1 s process
```
Disabling GC is worth 5-15% on parse-heavy runs. `sys.exit` frees memory anyway.

2. `parse_patch`: nothing needed. (Only the optional unit-level trick in (d) needs `new_start`.)

3. New helpers (near `def_names`):
```python
def fingerprint(tree) -> tuple:
    s = ast.dump(tree).encode()
    return (len(s), zlib.crc32(s), zlib.adler32(s))

def file_facts(tree) -> dict:
    """Per-file facts the selector needs from the ORIGINAL tree (computed at build for sqlglot/ files)."""
    stmts = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt) or hasattr(node, "body") or not node.end_lineno:
            continue
        if node.end_lineno - node.lineno >= 40:
            continue
        lams = tuple((n.lineno, n.end_lineno) for n in ast.walk(node) if isinstance(n, ast.Lambda))
        stmts.append((node.lineno, node.end_lineno, lams))
    stmts.sort()
    return {"stmts": tuple(stmts), "starts": tuple(s[0] for s in stmts),
            "defs": frozenset(def_names(tree)), "fp": fingerprint(tree)}
```

4. `Selector.__init__`: replace lines 152-168 (gzip/json/INDEX) with a marshal load.
```python
BAKE = Path(os.environ.get("BAKE", APP / "maps/bake.bin"))

# in __init__, replacing the gzip/json block:
        with open(BAKE, "rb") as fh:
            b = marshal.loads(fh.read())
        number_of = [self.by_id.get(tid) for tid in b["tests"]]
        self._number_of = number_of
        self._raw_sets = b["sets"]
        self._sets: dict[int, frozenset] = {}
        self.files = b["files"]                      # rel -> (lines{int:sid}, ranges)
        self.index = b["index"]
        self._perfile = b["perfile"]                 # rel -> marshal bytes, loaded lazily
        self._facts: dict[str, dict] = {}
        self.string_sets = {number_of[k]: v for k, v in b["strings"].items() if number_of[k] is not None}
        self.strings = self.string_sets              # used only for `any(stem in s for s in strings)` iteration
        self._src_cache = {}
```
`fixture_change` iterates `self.strings.items()` with `any(stem in s for s in strings)` (works on a frozenset). Nothing else needs lists. The old `test_strings` list form is not required.

5. `cov`, `file_cov`, `innermost`, `class_tests`, `new_method_tests` (int keys, set-id dedup):
```python
    def sids(self, rel, lines) -> set:
        e = self.files.get(rel)
        if e is None:
            return set()
        t = e[0]
        return {t[ln] for ln in lines if ln in t}

    def union(self, sids) -> set:
        out: set[int] = set()
        for s in sids:
            out |= self.test_set(s)
        return out

    def cov(self, rel, lines) -> set[int]:
        return self.union(self.sids(rel, lines))

    def file_cov(self, rel) -> set[int]:
        e = self.files.get(rel)
        return self.union(set(e[0].values())) if e else set()

    def innermost(self, rel, line):
        fn = cls = None
        for start, end, kind in (self.files[rel][1] if rel in self.files else ()):
            if start <= line <= end:
                if kind == 0: fn = (start, end)
                else: cls = (start, end)
        return fn, cls

    def class_sids(self, name) -> set:
        subs, cs = self.index.get("subs", {}), self.index["class_sids"]
        names, todo, out = set(), [name], set()
        while todo and len(names) < 400:      # same 400 cap as before, so same selection
            n = todo.pop()
            if n not in names:
                names.add(n)
                todo.extend(subs.get(n, ()))
        for n in names:
            out.update(cs.get(n, ()))
        return out

    def class_tests(self, name) -> set[int]:
        return self.union(self.class_sids(name))

    def new_method_tests(self, names) -> set[int]:
        fs, lower = self.index["fn_sids"], self.index["class_lower"]
        sids: set[int] = set()
        for _cls, name in names:
            sids.update(fs.get(name, ()))
            if name.endswith("_sql") and name[:-4] in lower:
                sids |= self.class_sids(lower[name[:-4]])
        return self.union(sids)
```
`files` is now a `(lines, ranges)` tuple per file. Also change the `if f"{d}{stem}.py" in self.files` test in `dialect_family`. That works unchanged since `in` is on the dict keys. `self.files.get(rel, {}).get("lines", {})` appears only in the old cov/file_cov/innermost, all replaced.

6. Facts accessor (no old parse for `sqlglot/` files):
```python
    def facts(self, rel: str, old: str, old_tree=None) -> dict:
        d = self._facts.get(rel)
        if d is None:
            raw = self._perfile.get(rel)
            d = marshal.loads(raw) if raw else file_facts(old_tree if old_tree is not None else ast.parse(old))
            self._facts[rel] = d
        return d
```

7. `python_change` (selector.py:276-297), replacing the old-tree parse and dump block:
```python
        try:
            new_tree = ast.parse(new) if new else None
        except SyntaxError:
            ...unchanged...
        facts = None
        old_tree = None
        if old:
            facts = self._facts.get(f["old"]) or (marshal.loads(self._perfile[f["old"]])
                                                   if f["old"] in self._perfile else None)
            if facts is None:                      # tests/*.py and anything not baked
                try:
                    old_tree = ast.parse(old)
                except SyntaxError:
                    return set(self.all), "old-syntax-error"
                facts = file_facts(old_tree) if rel.startswith("sqlglot/") else None
            if facts is not None:
                self._facts[f["old"]] = facts
        if old and new_tree is not None:
            same = (facts["fp"] == fingerprint(new_tree)) if facts else \
                   ast.dump(old_tree) == ast.dump(new_tree)
            if same:
                return set(), "ast-identical"
        if rel.startswith("tests/"):
            return self.test_file_change(f, old, old_tree), "test-file"
        if not rel.startswith("sqlglot/"):
            return set(self.all), "other-python"
        return self.sqlglot_change(f, facts, new_tree)
```
A syntax error in the new tree still returns before this. `facts` is always set for baked sqlglot files. If a `sqlglot/` file is not in `_perfile` the fallback computes it, and `file_facts` costs the old walk once.

8. `test_file_change`: take `old_tree` and skip the second parse.
```python
    def test_file_change(self, f, old, tree=None):
        ...
        tree = tree or ast.parse(old)
```

9. `sqlglot_change` and `changed_statement_lines` with baked facts.
```python
    def sqlglot_change(self, f, facts, new_tree):
        ...
        changed = changed_statement_lines(facts, touched_old_lines(f))
        ...
        new_names = def_names(new_tree) - facts["defs"]

def changed_statement_lines(facts, touched):
    out = set(touched)
    stmts, starts = facts["stmts"], facts["starts"]
    for ln in touched:
        i = bisect.bisect_right(starts, ln) - 1
        inside_stmts = []
        while i >= 0 and starts[i] >= ln - 40:     # spans are < 40 lines
            a, b, lams = stmts[i]
            if a <= ln <= b:
                inside_stmts.append(stmts[i])
            i -= 1
        for a, b, lams in inside_stmts:
            if lams and any(x <= ln <= y for x, y in lams):
                continue                           # edit inside a lambda: recorded on the lambda's own lines
            out.add(a)
    return out
```
Check against the original at selector.py:~455-475. There `inside` was all touched lines in the statement, and the stmt was skipped only if **all** of them fell in lambdas. The per-line form above can add `a` when one touched line is outside a lambda and another is inside it. The original would also add it in that case, since one line is outside. Per-line logic is therefore equivalent: a statement is added iff at least one of its touched lines is not in a lambda. Keep it.

### What this saves (estimates)
| item | now | after |
|---|---|---|
| fixed load (gzip+json+index, main-script compile) | 170-220 ms | 50-70 ms (marshal of ~7 MB, imports ast/os/re/marshal/zlib/bisect) |
| per touched `.py` KB | ~4 ms | ~1.9 ms (new parse 0.3 + new dump 1.2 + new `def_names` 0.4) |
| `class_tests` on `Expression` etc. | 0.2-0.8 s each | 5-20 ms (set-id union only) |
| 30 sample patches | ~3-4 s | ~1.2-1.5 s |

Roughly 2 s saved on the 30 sample commits, or about 0.07 s per commit. The change makes no difference to the chosen tests.

## (d) Ranked recommendations

1. **Precompute `class_sids` / `fn_sids` in the bake and union set ids once** (items 4-5). Biggest tail risk: one `Expression` hit costs up to 0.8 s of CPU, which equals 40% of one 2 s worker. Zero recall impact, identical results. Saves 0.5-2 s on the sample.
2. **Bake the old-file facts and skip the old parse, old dump and old walk** (items 3, 6, 7, 9). Cuts about half of the per-patch parse cost. A `parser.py` patch drops from about 1.7 s to about 0.8 s. Saves about 1 s on the sample. Identity is by a 64-bit-plus-length fingerprint; the false-identical probability is negligible.
3. **Marshal bake of map + index** (Dockerfile, bake.py, item 4). Saves about 100-150 ms per run, fixed.
4. **Move the code to `blast.py` and keep `selector.py` a stub** so the compiled `.pyc` is actually used. Saves 15-25 ms per run. Also use `--invalidation-mode unchecked-hash`.
5. **`gc.disable()`** at the top of `blast.py`. About 5-15% of parse time. One line.
6. **`test_file_change` reuses `old_tree`** (item 8). Saves one parse of a test file (`tests/test_parser.py` or `tests/dialects/*.py`, 100-400 KB, so 30-120 ms) per patch.
7. Optional, not recommended unless profiling shows `ast.dump(new)` dominates: skip the whole-file dump of the new tree. Design: compute the change runs from the hunks. Each run is the old lines `[o-1, o_after]` and the new lines `[n-1, n_after]`, one context line either side. Fingerprint only the baked units that overlap a run on each side, and compare the two lists. A unit is a top-level statement, a function, or a class header and each of its members. Untouched units are textually identical and ordered the same, so the equality is sound. It needs `new_start` in the hunk dict (`HUNK` group 3) and a unit flattener. It saves another ~1.2 ms per KB, about 0.5 s on the sample. Expect to write about 40 more lines, with some risk of a subtle bug; the dump of the single giant `class Parser` is why top-level-only units would not help.

Not worth doing: swapping `pathlib` for `os.path` (about 5-8 ms), lazy-loading `re` (about 5 ms), or caching parses across patches (each patch is a different file or text; the sample has almost no repeated `old` paths beyond `clickhouse.py` x3, and the baked facts already cover the old side).

Verification once rules allow running: compare `selector.py` output before and after the bake on the 30 samples (identical lines expected), then check `strace -c` or `/usr/bin/time` for CPU. I did not run any of this.
