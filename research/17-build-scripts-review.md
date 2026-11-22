# 17: Review of build scripts (build_index.py, build_map.py, Dockerfile)

Scope: `tools/build_index.py`, `tools/build_map.py`, `Dockerfile`, and how their output is used by `selector.py`.
Method: I read the code and inspected `work/sqlglot` with grep only. Nothing was run (BRIEF rule 1). The sizes below are grep estimates, not real build output.

## (a) Findings

### A1. The index shapes match what selector.py expects. No shape bugs.
| key | build_index.py | selector.py consumer | ok |
|---|---|---|---|
| toplevel | `file -> [[start,end,name]]` (:56) | `top_class` unpacks `start,end,name` (:223) | yes |
| lambdas | `file -> [[start,end]]` (:73) | `lambda_range` unpacks `start,end` (:245) | yes |
| refs | `name -> [[file,l,l]]` + `_sql` ranges (:111, :115) | `class_tests` unpacks `rel,start,end` (:239) | yes |
| subs | `name -> sorted[names]` (:121) | `class_tests` (:236) | yes |
| rdeps | `file -> sorted[files]` (:122) | `dialect_family` (:219) | yes |
| fns | `name -> [[file,start,end]]` (:67) | `new_method_tests` (:342) | yes |
Every class name gets a `refs` key (`setdefault`, :114-115), so `lower_class` in selector.py:339 is complete. No two class names collide when lowercased (checked the 1250 top-level names).

### A2. Relative-import resolution is correct, but no code path uses it
The code at build_index.py:89-94 is right for both modules and `__init__` packages, at any level. However, `grep -rn 'from \.' sqlglot` finds no relative imports at the pin, so this branch never runs. Two smaller points about absolute imports:
- `from sqlglot import exp` produces the targets `sqlglot` and `sqlglot.exp`. `sqlglot.exp` does not resolve to a file (it is an alias at `sqlglot/__init__.py:16`), so `sqlglot/expressions/__init__.py` gets no rdeps from roughly 180 importers. This does not matter today, because `dialect_family` only follows rdeps inside `DIALECT_DIRS` (selector.py:219).
- `ast.walk` (:83) also picks up imports under `if t.TYPE_CHECKING:` blocks. For example, `parsers/athena.py:11-12` imports `dialects.hive` and `dialects.trino` that way, so athena joins the hive and trino families. This only adds cost, and the cost is small.

### A3. The lambda visit is correct for class tables
`visit` (:64-76) does not set `in_fn` when it enters a `ClassDef`, so lambdas in nested class tables are recorded. Lambdas inside defs and inside other lambdas are left out. There are two blind spots that do not matter: lambdas in decorators and in default arguments are evaluated at definition time but are marked `in_fn` (grep finds none in sqlglot). `fns` uses `child.lineno`, which leaves out decorator lines (:67). That is harmless, because coverage puts nothing on decorator lines.

### A4. RECALL RISK: the selector's lambda shortcut is line-based, but 578 class-table lines put the dict key on the same line as the lambda
`grep -E '^\s+exp\.[A-Za-z]+: lambda'` gives 578 lines. In selector.py:311-317, any changed line inside a lambda's line range takes only the coverage of that lambda. Two edit shapes get this wrong:
1. **A new entry inserted between two lambda entries**, for example adding `exp.Foo: lambda self, e: ...` to `TRANSFORMS`. `touched_old_lines` (:89-90) marks only the two neighbouring old lines, and both are lambdas. `changed_statement_lines` does not widen the line set: the dict spans 40 lines or more (:447), or every touched line is inside a lambda (:454). The selector then keeps only the tests that called the *neighbouring* lambdas and misses every test that renders `exp.Foo` in that dialect.
2. **The key on a lambda line is edited**, for example changing `exp.ArrayAgg:` to `exp.ArrayConcat:`. The selector keeps the tests that called the old lambda and misses the tests for the new key.
This is a selector bug, but its inputs come from the index. The fix is C1.

### A5. Refs tokenizing: typing names cause false matches, and same-name classes are merged into one key
- Tokens are matched by NAME only (:107), so `t.Any` (184 lines), `t.Literal`, `t.Tuple`, `t.Set`, `t.Union` and others count as refs of `exp.Any` (core.py:1634), `exp.Literal` (core.py:1764), and so on. The covered function-body lines with these annotations make `class_tests("Any")` very large. This only adds cost.
- There are 8 duplicate top-level class names: `Generator` (generator.py:92 and expressions/array.py:240), `Table`, `Join`, `Sort`, `SetOperation`, `Schema`, `Insert`, `Update`. `subs` is keyed by the bare base name (:59-61), so a class-body edit to `exp.Generator` walks into the 21 dialect generators that subclass `generator.Generator` and everything under them. This only adds cost.
- The rest works as intended: strings and comments are left out, and names inside f-strings are still tokens on 3.12+.
- grep estimate: there are at most 25,622 unique (file, line, name) hits for the 1250 class names. The biggest are `TokenType` (2927), `Expr` (1582), `DType` (1521) and `Expression` (927).

### A6. Failure modes that can still break the image build
1. **Dockerfile:7-13.** `apt-get update` and `install git` download from `deb.debian.org`. The README (line 48) lists the allowed sources as "PyPI, npm, Docker Hub, GitHub releases at a fixed tag, and sqlglot's own repository at the pinned commit". A Debian mirror is not in that list, and the apt index is not pinned. This is a possible rule violation and a dependency on mirror uptime. Any transient apt or git network error also fails the `RUN` step, with no retry.
2. **build_index.py:131-134.** The error handling covers the whole `build()` call. One `RecursionError` in a deeply nested AST, raised by `visit` or `ast.walk`, empties *the entire* index without notice. The selector then falls back to broad rules everywhere. `out.write_text` sits outside the `try`, so a missing directory or an I/O error exits non-zero and fails the build.
3. **Dockerfile:19-20.** `build_index && chmod && compileall` is one chain, so a build_index crash, even a segfault, also skips the `chmod`. `compileall` exits non-zero on a syntax error in selector.py. That is acceptable, because the image would be useless anyway. However, the pyc it writes is **never used**: `python /app/selector.py` runs the file as `__main__`, and Python does not load a cached pyc for the main script.
4. Parsing sqlglot sources under 3.13: the pin requires Python 3.9 or later, and there is no 3.14-only syntax (no unparenthesised `except A, B`), so `ast.parse` does not hit the skip at :41.

### A7. The README build constraints are met, apart from the network rule in A6.1
- **Readable by `nobody`**: yes. `cp -r` uses umask 022, and `chmod -R a+rX /app` runs after the index is written. `__pycache__` is created later, but also with 644/755.
- **Read-only disk at runtime, 64 MB `/tmp`**: yes. The selector writes nothing, and `PYTHONDONTWRITEBYTECODE=1` is set.
- **10-minute build**: yes. apt plus git takes about 30-60 s, and build_index (184 files, 88k lines, `ast.parse` plus `tokenize`) takes a few seconds.
- **Repo size**: `work/` is in `.dockerignore`, and the 770 KB map is well under 20 MB.

### A8. Size of index.json and its load cost (estimated)
- refs: about 22-25k entries at about 40 B each, which is about 0.9-1.0 MB.
- fns: 3575 defs, about 0.18 MB.
- toplevel: 1258 entries, about 0.06 MB.
- lambdas, subs and rdeps: under 0.1 MB together.
- **Total: about 1.2-1.4 MB of JSON.** At runtime it costs about 15-30 ms of charged CPU per run, for one eager `json.loads` (selector.py:146).

For comparison, `impact.json.gz` decompresses to **7.05 MB** (measured, with 8966 sets and 53,696 line entries) and is the main load cost. The index adds about 15-20% to it. About 55% of refs is repeated file paths and the duplicated `l,l` end line.

### A9. build_map.py
- **Latent bug at :125-127.** `pickle_lines["sqlglot/expressions/core.py"]` is overwritten on each loop pass, so only the last of `__reduce__`, `__reduce_ex__`, `__getstate__` and `__setstate__` survives. Today only `__reduce__` exists (core.py:890), so the bug has no effect yet.
- **Silent failure at :94.** If coverage was recorded under a different absolute root, every `relpath` starts with `../` and is skipped, which gives an empty map with no error. Add an assertion (C5).
- Test lines get only `ranges` (test_ranges, :159), and lambdas get no range. Both are fine, because the selector gets lambdas from the index.

## (b) Recall risks and cost waste
| # | patch shape | effect under current code |
|---|---|---|
| R1 | Insert a `exp.X: lambda ...` entry between two lambda entries in a generator, parser or typing table | **miss**: keeps only the neighbouring lambdas' tests (A4) |
| R2 | Change the dict key on a `exp.X: lambda` line | **miss** for tests of the new key (A4) |
| R3 | A network or apt error during build, or apt ruled out-of-policy | rejected or failed image (A6.1) |
| R4 | One file deep enough to raise `RecursionError` in build_index | whole index empty, so broad fallback cost on every import-time edit (A6.2) |
| W1 | Class-body edit to `exp.Any`, `exp.Literal`, `exp.If` and similar | `t.Any`-style refs pull in large unrelated sets (A5) |
| W2 | Class-body edit to `exp.Generator`, `exp.Schema`, `exp.Table` and similar | name collision walks into unrelated subclass trees (A5) |
| W3 | Every run | about 20 ms to load the index and an unused pyc (A6.3, A8) |

## (c) Proposed changes

### C1 (selector.py, fixes R1 and R2). Take the lambda shortcut only when the edit stays inside that one lambda and keeps its key.
Add this method to `Selector`:
```python
    def lambda_only(self, f: dict, lam: tuple[int, int]) -> bool:
        """Every edit near `lam` stays inside that lambda's lines and keeps the text before `lambda` (the dict key)."""
        lo, hi = lam
        head = lambda s: s.split("lambda", 1)[0].strip()
        for h in f["hunks"]:
            rem = [t for tag, t, o in h["lines"] if tag == "-" and lo <= o <= hi]
            add = [(t, o) for tag, t, o in h["lines"] if tag == "+" and (lo <= o <= hi or lo <= o - 1 <= hi)]
            # An insertion with nothing of the lambda removed must fall strictly inside it.
            if not rem and any(not (lo < o <= hi) for _t, o in add):
                return False
            if {head(t) for t in rem if "lambda" in t} != {head(t) for t, _ in add if "lambda" in t}:
                return False
        return True
```
In `sqlglot_change`, change line 313 from `if hit:` to:
```python
                if hit and self.lambda_only(f, lam):
```
When the check fails, the code falls through to `import_time_change`, which uses family file coverage plus name tests. That is broad but safe.

### C2 (Dockerfile, fixes R3). Drop apt and git. Fetch the pinned tarball from sqlglot's own repository with retries, and never fail the build.
Add a new file `tools/fetch_src.py`:
```python
"""Download sqlglot at the pin (sqlglot's own repo) into <dest>/{sqlglot,tests}. Never fails the build."""
import io, sys, tarfile, time, urllib.request
pin, dest = sys.argv[1], sys.argv[2]
url = f"https://codeload.github.com/tobymao/sqlglot/tar.gz/{pin}"
data = None
for attempt in range(6):
    try:
        data = urllib.request.urlopen(url, timeout=90).read()
        break
    except Exception as e:
        print(f"fetch attempt {attempt}: {e}", file=sys.stderr)
        time.sleep(5 * (attempt + 1))
if data is None:
    print("fetch failed: selector will run every test", file=sys.stderr)
    sys.exit(0)
with tarfile.open(fileobj=io.BytesIO(data)) as tf:
    members = tf.getmembers()
    top = members[0].name.split("/")[0] + "/"
    keep = [m for m in members if m.name.startswith((top + "sqlglot/", top + "tests/")) and (m.isfile() or m.isdir())]
    for m in keep:
        m.name = m.name[len(top):]
    tf.extractall(dest, members=keep, filter="data")
```
Replace Dockerfile:6-13 with:
```dockerfile
ARG PIN=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
COPY tools/fetch_src.py /app/tools/fetch_src.py
RUN mkdir -p /app/src && python /app/tools/fetch_src.py $PIN /app/src \
 && (test -f /app/src/sqlglot/__init__.py || echo "WARNING: no sqlglot sources" >&2)
```
python:3.13-slim already includes `ca-certificates`. This change also removes about 30-60 s of apt time and the git layer.

### C3 (Dockerfile:19-20, fixes the A6.3 chain). Never let the index step fail the build, and drop the unused compileall.
```dockerfile
RUN (python /app/tools/build_index.py /app/src /app/maps/index.json || echo "index step failed" >&2) \
 && chmod -R a+rX /app
```
Optional, to save about 5-10 ms of compile per run: add `/app/run.py` containing `import sys; sys.path.insert(0, "/app"); import selector; selector.main()`, keep `python -m compileall -q /app/selector.py`, and set `CMD ["python", "-S", "/app/run.py"]`. The pyc is then actually used.

### C4 (build_index.py, fixes R4 and W1). Make failures per-file, always write the output, and drop `t.X` refs.
- At the top of `build()`: `sys.setrecursionlimit(20000)`.
- Wrap the body of `for rel, tree in trees.items():` (:52-99) in `try: ... except Exception as e: print(f"index skip {rel}: {e}", file=sys.stderr)`.
- Replace the tokenize loop at :104-111 with:
```python
            toks = tokenize.generate_tokens(io.StringIO(text).readline)
            seen, prev = set(), ["", ""]
            skip_types = (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT)
            for tok in toks:
                if (tok.type == tokenize.NAME and tok.string in class_names
                        and not (prev[1] == "." and prev[0] in ("t", "typing"))):
                    key = (tok.string, tok.start[0])
                    if key not in seen:
                        seen.add(key)
                        refs.setdefault(tok.string, []).append([rel, tok.start[0], tok.start[0]])
                if tok.type not in skip_types:
                    prev = [prev[1], tok.string]
```
- Replace `main()`'s write (:134-135) with:
```python
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(index, separators=(",", ":")))
    except Exception as e:
        print(f"build_index could not write {out}: {e}", file=sys.stderr)
    print(f"index: {', '.join(f'{k}={len(v)}' for k, v in index.items())}", file=sys.stderr)
```

### C5 (build_map.py). Fix the latent pickle bug and fail loudly on a root mismatch. This script runs offline, so it may fail.
Replace :125-127 with:
```python
    pickle_core = set()
    for node in ast.walk(core):
        if isinstance(node, ast.FunctionDef) and node.name in ("__reduce__", "__reduce_ex__", "__getstate__", "__setstate__"):
            pickle_core.update(range(node.lineno, node.end_lineno + 1))
    if pickle_core:
        pickle_lines["sqlglot/expressions/core.py"] = sorted(pickle_core)
```
After the measured-files loop (:109), add:
`assert sum(bool(v["lines"]) for v in files.values()) > 100, "coverage paths do not match ROOT"`.

### C6 (build_index.py, fixes W2; optional). Keep subs edges only when the base resolves to the class's defining file.
After the toplevel loop, build `defs[name] -> [files]`. In the subs step, for a base `mod.Name`, resolve `mod` through the file's module-level `from X import mod` or `import X as mod` to a module file. Treat `from sqlglot import exp` as `sqlglot/expressions/`, the alias at `sqlglot/__init__.py:16`. Skip the edge when `Name` is defined elsewhere and the resolved module path is neither that defining file nor its package directory. That drops `DuckDBGenerator` and the other dialect generators from `subs["Generator"]` for `exp.Generator` edits (expressions/array.py:240). Keep the current behaviour when the base cannot be resolved.

### C7 (optional, load cost). Use a compact refs format.
Emit `refs: {name: {file: [line, ...]}}` and a separate `sqlm: {name: [[file, s, e]]}` for the `_sql` bodies. In `class_tests`, loop with `for rel, lines in refs.get(n, {}).items(): out |= self.cov(rel, lines)` and then the same over `sqlm`. This makes refs about 50% smaller, which saves about 8 ms per run. It is low priority next to the 7 MB impact map.

## (d) Ranked recommendations
1. **C1, the lambda key and insertion guard** (selector.py). This fixes two edit shapes that miss tests (R1, R2) on 578 class-table lines. Each miss costs 300 s. It adds a little cost only for edits that are not pure lambda-body edits. **High.**
2. **C2, drop apt and fetch the tarball with retries** (Dockerfile). This removes a possible policy rejection and a network single point of failure, and saves 30-60 s of build time. **High** (it only affects whether the image is accepted).
3. **C3 and C4, make the index step unable to fail and per-file robust.** This guarantees the build succeeds and stops one bad file from emptying the whole index. **Medium.**
4. **C4's refs filter and C6's subs disambiguation.** These cut wasted selections on class-body edits to `Any`, `Literal`, `If`, `Generator`, `Schema` and `Table` (W1, W2). **Low to medium.**
5. **C5** (build_map.py robustness). **Low**, because nothing is wrong today.
6. **C7 and the run.py pyc.** Together these save about 15 ms of CPU per run. **Low.** The real load cost is the 7 MB map, which is outside this review.
