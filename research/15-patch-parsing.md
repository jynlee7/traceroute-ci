# 15 - Robustness of parse_patch / touched_old_lines / apply_patch (selector.py)

No code was run. Everything below is a by-hand walk of selector.py (line numbers are from the current file).

## (a) Findings

### Sample format
All 30 sample patches are plain `git diff` output with 3 lines of context. A grep over them found:
- 35 hunks, every header has both counts, none has `,0`.
- No `new file`, `deleted file`, `rename`, `similarity`, mode lines, `Binary files`, or `\ No newline`.
- No bare empty lines, no CRLF, no `---`/`+++` lines inside hunks.

So the sample never exercises any rarer shape listed below. The hidden set may.

### Case-by-case walk (current behavior)

1. New file (`--- /dev/null`).
   - Line 57 sets old to None. `python_change` (267) uses old="" and `apply_patch("", f)`.
   - For `@@ -0,0 +1,N @@`, start=0 (L101), and the `""` element is appended at L119, so there is a trailing newline. This is correct.
   - sqlglot/ files return "file-added-or-removed", which is all tests (safe). A new tests/ .py file is not in by_file, so it returns all (safe).
   - A new fixture goes through `fixture_change` (373), which can pick a subset. See R5.

2. Deleted file (`+++ /dev/null`).
   - new is None, so `new=""` (L270). sqlglot/ files return "file-added-or-removed" (safe). tests/ files go through `test_file_change`, using path=old. This is fine.

3. Header-only diffs: pure rename, mode-only change, binary. This is a real bug.
   - With no `---`/`+++` lines, old and new stay None. Lines 74-76 then set both to `""`, and the comment promises "take the path from the git line", but the code never does.
   - So `f["path"] == ""` (L77).
   - **Binary (L393-394)**: `Binary files a/x and b/x differ` sets binary=True. The path is `""`, which does not start with `sqlglot/` or `tests/`, so the result is `set()` with reason "binary-other". **A changed binary under tests/ or sqlglot/ selects nothing. That is a silent miss.**
   - Mode-only and pure rename have suffix `""`, so they reach the "other" branch and return all tests. This is safe but costly. A chmod on any file costs a full run.
   - `GIT binary patch` (from `--binary`) is not detected. It also ends up at path `""`, so it returns all by accident.
   - Danger if the path is fixed naively: a 100% rename `sqlglot/a.py` -> `sqlglot/b.py` would have no hunks. `apply_patch` returns the old text, so `ast.dump` is identical and the result is `set()` with "ast-identical". That would be a miss. Any path fix must be paired with a rename rule (see fix F1).

4. Rename with edits (`similarity index 90%`, `rename from/to`).
   - Hunks are numbered against the old file. `python_change` reads `source(f["old"])` correctly. But `sqlglot_change` looks up coverage with `rel = f["path"]` (the NEW path), so `files.get(rel)` is empty.
   - The result is the "module-level-unmapped" return of all (L323-324). That is safe by accident. The same happens for tests/: `rel in self.by_file` is false, so it returns all.

5. `\ No newline at end of file`.
   - The `\\` branch (L71) skips the marker. The marker does not change `old_no`.
   - Adding or removing the final newline of a .py file gives identical text lines. The AST is identical, so `set()`. This is correct, because Python does not care.
   - A .sql file takes the stem lookup, which is safe.

6. CRLF.
   - Both `patch.read_text` (L490) and `source()` (L154) use universal newlines, so `\r\n` becomes `\n` on both sides. They stay consistent with each other and with coverage.py's line numbering. No bug.
   - Lone `\r` (old-Mac) would be split by `read_text` but not by git. This is theoretical and not worth fixing.

7. Tabs and spaces in paths.
   - L56 `split("\t")[0]` correctly handles git's trailing tab after paths with spaces, and `diff -u` timestamps.
   - Git C-quotes paths with a real tab, a `"`, or non-ASCII (core.quotePath), for example `--- "a/caf\303\251.py"`.
   - The code only strips `a/` when the string starts with it. A quoted path stays `"a/caf\303\251.py"`, so `source()` finds nothing and returns "unknown-file", which is all. For a non-.py file the suffix becomes `.sql"`, which hits "other" and also returns all. This is safe but costly. sqlglot has no such paths, so it is low priority.
   - Mnemonic prefixes (`i/`, `w/`, `c/`) are not stripped, so the result is all (safe).

8. `---`/`+++` inside hunk content.
   - L55 and L58 are guarded by `not cur["hunks"]`. After the first `@@`, a removed SQL comment line `--- foo` falls to L66 and is stored as `("-", "-- foo", n)`. An added `+++ x` is stored as `("+", "++ x")`. This is correct.
   - `sql_comment_only` (L428) sees `"-- foo"` and treats it as a comment. This is correct.
   - The guard breaks only if a hunk's lines are dropped before the first `@@`, which does not happen.

9. Hunk-end / trailer handling. The parser never tracks the hunk counts, so a hunk only ends at the next `@@` or `diff --git`.
   - `git format-patch` trailers `-- \n2.43.0\n` are swallowed as a removal of the line `"- "` (L66). `apply_patch` then fails ("patch-did-not-apply"), which is all (safe but costly). For .sql files it adds a spurious non-comment line, which runs tests (safe). `touched_old_lines` also gets a bogus line past EOF.
   - A bare empty line inside a hunk (some mail clients and editors strip the single space of an empty context line) is dropped. `old_no` is not advanced, so every later `touched` number is off by one. `apply_patch` will usually fail on the next context line, which is safe. A hunk whose remaining lines are all `+`/`-` with no context could apply and give a wrong touched set. This is rare.

10. Multiple hunks. `apply_patch` handles them in order: `out.extend(src[pos:start])` and `start < pos` returns None (L103). This is correct for old-numbered hunks.

11. Hunk at line 0 and `@@ -1 +1 @@` without counts.
    - HUNK (L33) makes the counts optional. The counts are unused, so `old_start=1` is fine.
    - `-0,0` is handled by L101. `touched_old_lines` adds `0` and `max(1,-1)=1`. A `0` is harmless, because it is only emitted for new files, which return before use.

12. **Zero-length old range with context (`-U0`, or `git diff -U0`; the hunk is `@@ -5,0 +6,2 @@`). This is a real bug.**
    - In unified diff, when the old count is 0, `old_start` is the line BEFORE the insertion (the insertion happens after old line 5).
    - `apply_patch` (L100) uses `start = old_start-1 = 4`, so it inserts before old line 5. That is one line too early. The resulting tree can be wrong: it may hit "syntax-error" (all, safe), or it may reorder across a block boundary and produce a wrong `ast-identical` or a wrong def set.
    - `touched_old_lines` seeds `old_no = 5` and, for `+`, adds `{5, 4}`. The truth is `{5, 6}`, because the line after the insertion is old line 6. The line following the insertion is missed. If that line is the first line of a function, or of the next statement, the window may land on lines whose coverage differs.
    - A pure insertion with `-U0` is not produced by the 3-context samples, but a fresh `git diff -U0` or a `git format-patch -U0` would trigger it. Pure deletion with `-U0` (`@@ -7,2 +6,0 @@`) is fine, since old_start is real there.

13. Non-UTF-8 bytes.
    - The patch is read with `errors="replace"` (L490). The source is read strictly (L154), so any non-UTF-8 source file raises `UnicodeDecodeError`. That is caught at L491 and falls back to all tests. It is safe, but one bad file inflates the whole commit.
    - There is no per-file isolation. Also, a U+FFFD in a context line of the patch does not match the strict-decoded source, but that case cannot happen with valid-UTF-8 sources.

14. Empty or unparsable patch (**silent miss**).
    - Anything before the first `diff --git` is dropped (L53). A plain `--- a/x`/`+++ b/x` patch with no `diff --git` line (plain `diff -u`, `patch` emails) returns `[]`.
    - `diff --cc` / `@@@` combined diffs also return `[]`, and their hunks have no `@@ -` header.
    - `select([])` returns `(set(), [])`, so **the commit gets zero tests** and `split` returns `[]`.

15. Series patches (two commits in one file, both touching the same file) give two entries for the same path. The second entry's hunks are numbered against the intermediate state, so `apply_patch` usually fails (safe: all). It could silently apply in a degenerate case. This is low priority.

## (b) Recall risks (patch shapes that miss) and cost waste

Misses:
- M1. Binary change under `tests/` or `sqlglot/` (finding 3). It selects zero tests. Example: a `.pkl`, `.parquet`, `.gz`, or `.db` fixture.
- M2. A patch with no `diff --git` header, or a combined diff (finding 14). It selects zero tests.
- M3. A 100% rename, if someone only fixes the path (finding 3). The rule below prevents it.
- M4. `-U0` pure-insertion hunks (finding 12): wrong touched lines and a wrongly placed insertion in the applied text. This is a partial miss.
- M5. New fixture file read by directory glob (finding 1): `fixture_change` (L379-384) matches by stem or parent-dir name in test strings, so a new file in a directory that a test lists with `glob`/`listdir` is not found by stem. The parent-dir fallback may still catch it. I did not verify which tests glob.

Cost waste (safe all-test fallbacks):
- Mode-only and pure-rename diffs (all tests). Quoted paths (all tests). Mail trailers (all for .py). One non-UTF-8 file (all). Renames with edits (all).

## (c) Proposed code changes

### F1. Take paths from the git header; handle new/deleted/rename/mode/binary. Replace `parse_patch` L43-78 as follows.

```python
def _unquote(p: str) -> str:
    """Undo git's C-style quoting of a path ("a/caf\\303\\251.py")."""
    if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
        try:
            return p[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape").encode("latin-1").decode("utf-8", "replace")
        except Exception:
            return p[1:-1]
    return p


def _strip_prefix(p: str, pre: str) -> str:
    p = _unquote(p.split("\t")[0])
    return p[2:] if p[:2] in (pre, "a/", "b/", "i/", "w/", "c/", "o/") and len(p) > 2 and p[1] == "/" else p


def _git_line_paths(rest: str):
    """`a/P b/P` (same path both sides) -> (P, P); otherwise (None, None) and rename lines decide."""
    mid = len(rest) // 2
    if len(rest) % 2 == 1 and rest[mid] == " ":
        a, b = rest[:mid], rest[mid + 1:]
        if a[2:] == b[2:]:
            return _strip_prefix(a, "a/"), _strip_prefix(b, "b/")
    return None, None


def parse_patch(text: str) -> list[dict]:
    files: list[dict] = []
    cur = None
    old_no = 0
    left_old = left_new = 0  # lines still owed by the current hunk
    for line in text.split("\n"):
        if left_old > 0 or left_new > 0:
            # Inside a hunk: every line is content, whatever it starts with.
            c = line[:1]
            if c == "\\":
                continue
            if line == "":
                c, line = " ", " "  # an empty context line whose space was stripped
            if c in (" ", "-", "+"):
                cur["hunks"][-1]["lines"].append((c, line[1:], old_no))
                if c != "+":
                    old_no += 1
                    left_old -= 1
                if c != "-":
                    left_new -= 1
                continue
            left_old = left_new = 0  # malformed: fall through and treat as a header
        if line.startswith("diff --git "):
            a, b = _git_line_paths(line[11:])
            cur = {"old": a, "new": b, "hunks": [], "binary": False, "git": True,
                   "created": False, "deleted": False, "renamed": False, "mode_only": True}
            files.append(cur)
            continue
        if line.startswith("diff --cc ") or line.startswith("diff --combined "):
            cur = {"old": None, "new": None, "hunks": [], "binary": True, "git": False,
                   "created": False, "deleted": False, "renamed": False, "mode_only": False}
            files.append(cur)  # unreadable: treated as binary below, i.e. everything
            continue
        if cur is None:
            continue
        if line.startswith("new file mode"):
            cur["created"] = True
        elif line.startswith("deleted file mode"):
            cur["deleted"] = True
        elif line.startswith("rename from "):
            cur["old"] = _unquote(line[12:]); cur["renamed"] = True
        elif line.startswith("rename to "):
            cur["new"] = _unquote(line[10:]); cur["renamed"] = True
        elif line.startswith("--- ") and not cur["hunks"]:
            p = line[4:].split("\t")[0]
            cur["old"] = None if p == "/dev/null" else _strip_prefix(p, "a/")
            cur["mode_only"] = False
        elif line.startswith("+++ ") and not cur["hunks"]:
            p = line[4:].split("\t")[0]
            cur["new"] = None if p == "/dev/null" else _strip_prefix(p, "b/")
            cur["mode_only"] = False
        elif line.startswith(("Binary files", "GIT binary patch")):
            cur["binary"] = True
        elif m := HUNK.match(line):
            old_start = int(m.group(1))
            n_old = int(m.group(2)) if m.group(2) is not None else 1
            n_new = int(m.group(4)) if m.group(4) is not None else 1
            # With an old count of 0 the start is the line BEFORE the insertion.
            old_no = old_start + 1 if n_old == 0 and old_start > 0 else old_start
            cur["hunks"].append({"old_start": old_no, "n_old": n_old, "lines": []})
            left_old, left_new = n_old, n_new
            cur["mode_only"] = False
    for f in files:
        if f["created"]:
            f["old"] = None
        if f["deleted"]:
            f["new"] = None
        if f["old"] is None and f["new"] is None and not f["created"] and not f["deleted"]:
            f["old"] = f["new"] = ""
        f["path"] = f["new"] or f["old"] or ""
    return files
```

Notes on F1:
- `old_start` stored in the hunk is now the index of the first old line the hunk touches (1-based). For `n_old == 0` that is `old_start + 1`, so `apply_patch` (`start = old_start - 1`) inserts after the right line. `apply_patch` and `touched_old_lines` then need no further change. The `old_start == 0` special case at L101 stays. In `touched_old_lines`, `+` lines now add `{n, n-1}`, which is the line after and the line before the insertion. This is correct.
- The `left_old/left_new` counters fix findings 9 and 8 for free, since trailers and `---` lines are never reinterpreted. A `-U0` header with counts 0 on both sides is impossible.
- A hunk with a missing count uses 1, which matches the unified-diff rule.

### F2. Decide on header-only diffs and renames in `select` (L390). Insert at the top of the loop body.

```python
        for f in files:
            rel = f["path"]
            suffix = Path(rel).suffix
            if f.get("renamed") or (f["old"] and f["new"] and f["old"] != f["new"]):
                # A moved file changes import paths and test ids: everything may break.
                s, r = set(self.all), "rename"
            elif f["binary"]:
                if rel.startswith(("sqlglot/", "tests/")) or not (suffix in DOC_SUFFIXES or rel.startswith(("docs/", ".github/"))):
                    s, r = set(self.all), "binary"
                else:
                    s, r = set(), "binary-doc"
            elif f.get("mode_only") and not f["hunks"]:
                # chmod only: nothing the tests can observe (python, sql, json alike).
                s, r = set(), "mode-only"
            elif not rel:
                s, r = set(self.all), "no-path"
            elif suffix == ".py":
                ...  # unchanged from here
```
The existing `if f["binary"]: ...` branch is replaced by the two blocks above, and the `elif suffix == ".py":` chain continues.

Careful: a new or deleted file with `new file mode` and no `---`/`+++` (an empty file) has `mode_only=True` and no hunks. It must not be treated as mode-only. Add the guard `and not f["created"] and not f["deleted"]` to the mode-only condition. With that guard an empty created/deleted file falls through to the normal chain (path is set; `python_change` returns "file-added-or-removed" for sqlglot/, all).

### F3. Never select nothing from a non-empty patch. Replace the body of the `try` in `main` (L490).

```python
        try:
            text = patch.read_text(errors="replace")
            files = parse_patch(text)
            if not files and text.strip():
                chosen, why = set(sel.all), ["unparsed-patch"]
            else:
                chosen, why = sel.select(files)
```
This covers findings 14, plain unified diffs, and combined diffs. An empty patch (`text.strip()` empty) is truly empty and stays `set()`.

### F4. Per-file isolation and tolerant source decoding.
In `source()` (L154):
```python
            self._src_cache[rel] = p.read_text(errors="replace") if p.is_file() else None
```
This keeps patch and source consistent for bytes that are not UTF-8, so `apply_patch` succeeds instead of raising.

### F5. Coverage lookup under rename (only matters if renames stop returning all)
Not needed after F2, since renames return all before reaching `sqlglot_change`.

### F6. Unit cases to run later (not now, by the brief's rules)
Hand-built patches for each of: `@@ -5,0 +6,2 @@` insertion; pure rename of a .py; chmod; `Binary files`; no-`diff --git` patch; `-- \n2.43.0` trailer; a removed `--- x` line in a .sql fixture; `@@ -1 +1 @@` without counts; a hunk with stripped empty context lines.

## (d) Ranked recommendations

1. **F1 + F2 binary/header-only branch (fixes M1)**. A binary under tests/ or sqlglot/ currently selects nothing, which is a 300 s penalty per miss. Highest priority and tiny change. Must ship together with the rename rule, otherwise a 100% rename becomes `ast-identical` -> `set()`.
2. **F3 (fixes M2)**. A one-line guard so a non-empty, unparsed patch runs everything instead of nothing. Zero cost on well-formed patches.
3. **Zero-count hunk fix (F1, `old_no = old_start + 1` when `n_old == 0`)**. Fixes the wrong insertion point in `apply_patch` and the wrong touched line (M4). Applies only to `-U0` diffs, so it is rare, but the error is silent.
4. **Hunk-count tracking (F1)**. Removes the trailer, stripped-empty-context, and `---` content ambiguities. It does not change the output on the sample (all hunks are well-formed with 3-line context), so it is a pure safety gain. Medium confidence that it is needed on hidden patches.
5. **F4 (`errors="replace"` in `source()`)**. Avoids all-test fallbacks from a single non-UTF-8 source file. Cost saving only.
6. **Quoted-path unquoting and mnemonic prefixes (F1 `_unquote`/`_strip_prefix`)**. Cost saving only, and low frequency for sqlglot paths.
7. **Mode-only returns `set()` (F2)**. Saves a full run on a chmod diff. Optional. If you want zero extra risk, keep it as all.

Sample impact: none expected. All 30 sample patches take the same code path after F1-F4, so the simulated score of 192.3 s should be unchanged. This must be checked by running the scorer, which I was not allowed to do.
