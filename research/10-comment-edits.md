# 10: Comment-only edits. Is "AST-identical" safe?

Scope: `selector.py` returns `[]` when `ast.dump(old) == ast.dump(new)` (selector.py:340), and returns `[]` for `.sql`
fixture edits whose changed lines are all empty or start with `--` (selector.py:432, :485-488). This note lists every way
an edit that looks like a comment can still fail a test here, checks each against the pinned sqlglot
(`work/sqlglot`), and proposes guards. Everything comes from reading the code: per the brief, nothing was executed.

## (a) Findings

### A. What the inert check actually compares
1. `ast.dump()` is called with default arguments (selector.py:340), so `include_attributes=False`: line and column
   numbers are **not** compared. A comment that adds or removes a line therefore shifts every later line number without
   changing the dump. That is only safe if nothing at test time depends on Python line numbers (see C).
2. `ast.parse(new)` / `ast.parse(old)` (selector.py:331, :337) run without `type_comments=True`, so `# type:` comments
   are invisible to the check. `Module.type_ignores` is always empty, so `# type: ignore` is invisible too.
3. `ast.parse()` on a **str** ignores PEP 263 coding cookies (CPython compiles str input with the cookie ignored),
   and the selector reads both the source and the patch as UTF-8 with `errors="replace"` (selector.py:211, :627).
   The real import decodes bytes using the cookie, with strict decoding.
4. The patch is read with `Path.read_text` (selector.py:627), which applies universal-newline translation: a lone
   `\r` inside a `+` line becomes a line break. `parse_patch` then silently drops the unprefixed remainder: mid-hunk it
   reaches `elif cur["hunks"]: continue` (selector.py:82-83); on the last hunk line it is treated as a trailer.

### B. The comment shapes the brief lists, checked one by one
| Shape | AST changes? | Selector outcome | Safe? |
|---|---|---|---|
| `#` inside a string literal (incl. SQL `--`/`/* */` inside a Python string) | yes, Constant value | goes on to coverage/test-file rules | yes, caught |
| Comment after a `\` continuation, or a `#` that swallows a `)` / `:` | SyntaxError | selector.py:332-335 → all (or the test file's tests) | yes |
| Comment-only line at odd indentation | no (the tokenizer skips comment-only lines) | `[]` | yes, harmless at runtime |
| Comment inside a 3.12+ multi-line f-string replacement field | no | `[]` | yes. The image runs Python 3.13 (Dockerfile:2), the same interpreter tests are labelled on (README.md:68) |
| `# type:` / `# type: ignore` | no | `[]` | yes: 38 `# type: ignore` in sqlglot, 0 real `# type:` annotations; nothing runs mypy (requirements.lock:13 installs it, but no test calls it; tests/test_sqlglotc.py:22 mocks `mypyc.build`) |
| `# noqa`, `# fmt: off`, `# pragma` | no | `[]` | yes: no test runs ruff, black or coverage (the only ruff config is pyproject.toml:47, which pytest never reads) |
| **Encoding cookie added or changed (line 1-2)** | **no** (finding A3) | **`[]`** | **NO, see risk R1** |
| **Invalid UTF-8 bytes in a comment** | no (bytes become U+FFFD) | **`[]`** | **NO, see R2** |
| **Lone `\r` in a comment line, followed by code** | no (remainder dropped, finding A4) | **`[]`** | **NO, see R3** |
| NUL byte in a comment | `ast.parse` raises SyntaxError on 3.12+ | all | yes |
| Docstring edits (they look like comments) | yes, Constant | coverage rules; module or class docstrings take `import_time_change` | safe, but wasteful (W1) |
| Comment edit plus a file rename | n/a | handled: `f["rename"]` → all (selector.py:456-458) | yes |
| `.sql` fixture: `--` or empty lines only | n/a | `[]` (selector.py:432) | yes, matches `_filter_comments` at tests/helpers.py:11-12, **except R4** |
| `.sql` fixture: `#` lines | n/a | not treated as comments → fixture-stem | correct: `#` lines are meta (tests/helpers.py:19-21, :39) |
| `.sql` fixture: trailing `-- x` after SQL, or an indented `  --` | n/a | not comment-only → fixture-stem | conservative and correct: the loader only drops lines that *start* with `--` |

### C. Is anything line-number or source-text sensitive? (searched `tests/` and `sqlglot/`)
- `inspect` in sqlglot: only `getmembers` (sqlglot/helper.py:153-155), `getfullargspec` (sqlglot/optimizer/optimizer.py:111)
  and `signature` (sqlglot/executor/env.py:101). None of them read source or line numbers. No `getsource`, `_getframe`,
  `f_lineno`, `co_firstlineno`, `linecache` or `traceback` anywhere in `sqlglot/` or `tests/`.
- tests/test_typing.py:41 uses `ast.parse` on `sqlglot/typing/*.py` (read at :60), but only to find the
  `ImportFrom ... EXPRESSION_METADATA` node, which comments cannot affect. `_same_code` (tests/test_typing.py:10-22)
  compares `co_code/co_names/co_varnames/co_consts` only. On 3.13 `co_code` carries no line table, so moving lines with
  comments cannot change the result. Docstrings *do* land in `co_consts`. This matters for W1.
- Warnings: one `warnings.warn` (sqlglot/tokens.py:21). It fires only if `sqlglotrs` is installed, and it is not in
  requirements.lock. No `filterwarnings=error` anywhere (no pytest config in pyproject.toml or setup.cfg). No
  `assertWarns` or `pytest.warns` in tests.
- Logs: `assertLogs` (tests/test_parser.py:495, :949; tests/test_expressions.py:118; tests/test_diff.py:306) compares
  `LEVEL:name:msg`, which has no line number. `assert_logger_contains` (tests/helpers.py:27-31) reads mock call args.
- Tracebacks: none are compared. tests/leakcheck.py is not a pytest test (it is not in tests.json).
- Doctests: tests/test_docs.py:9 uses unittest `load_tests`. pytest ignores it, and `sample/tests.json` has **0** entries
  for tests/test_docs.py, so docstring text never runs.
- `__doc__`: the only use is sqlglot/expressions/core.py:112, which sets it. No test reads it.
- Source files read as text by tests: only typing via test_typing. No test opens `.py` files under `sqlglot/` otherwise.
  The only `open()` calls in tests are fixtures (tests/helpers.py:35, :45), jsonpath (tests/test_jsonpath.py:69) and TPC CSVs
  (tests/test_executor.py:36-44).
- Sources that already contain non-ASCII characters (all inside comments): sqlglot/tokens.py:201,
  sqlglot/generator.py:3861, sqlglot/parsers/tsql.py:592, sqlglot/typing/redshift.py:43, among 33 files. No file has a
  coding cookie, and no `.py` or `.sql` file contains `\r`, `\x0b` or `\x0c`.
- `.sql` fixtures: every reader goes through `_filter_comments` (`str.splitlines()` plus `line.startswith("--")`):
  tests/test_transpile.py:1050/1098/1103, tests/test_optimizer.py:226/2078/3668/3956, tests/test_executor.py:118-119,
  tests/test_serde.py:24.
- The 3 sample commits labelled `comment` (9u5k, bap8, f4xa) each add one `#` line and fail nothing. They are consistent
  with the above. They are not used for tuning.

**Conclusion:** in this repo, an identical `ast.dump` *is* a sound inertness test for edits whose bytes really are a
comment in the file Python will import. No test depends on Python line numbers, comments or `# type:`/`# noqa`
text. The holes are all about **bytes the selector sees differently from Python or the fixture loader**: the coding
cookie, invalid UTF-8, a lone CR, and the extra line breaks `str.splitlines` honours. They are cheap to guard.

## (b) Recall risks and cost waste
- **R1 (miss): coding cookie.** `+# -*- coding: ascii -*-` as line 1 of sqlglot/tokens.py makes the import fail
  (non-ASCII byte at :201), which errors every test. `# coding: latin-1` silently mojibakes any non-ASCII string
  literal, and `# coding: bogus` gives "unknown encoding". In all three cases the AST is identical, so the selector
  returns `[]`. A miss costs 300 s.
- **R2 (miss): non-UTF-8 byte in a comment** (for example a Latin-1 `é` typed by an editor). Import raises a SyntaxError
  ("can't decode byte"). The selector sees U+FFFD in a comment, the AST is identical, so it returns `[]`. In a `.sql`
  fixture, the strict `open(..., encoding="utf-8")` (tests/helpers.py:35/45) raises UnicodeDecodeError, but
  `sql_comment_only` says `[]`.
- **R3 (miss): lone `\r`.** `+    # note\rraise ValueError` in a `.py` file (Python treats a lone CR as a newline), or
  `+-- note\rSELECT broken` in a `.sql` fixture. `read_text` splits the line, the second half is dropped, and the edit
  looks inert.
- **R4 (miss, `.sql` only): `str.splitlines` breaks.** `_filter_comments` splits on `\x0b \x0c \x1c \x1d \x1e \x85
     ` as well as on newlines. `+-- note SELECT 1` is a `--` line to the selector but two lines to the
  loader, so the second becomes a fixture statement. In a `.py` comment these characters are harmless (Python breaks
  lines only on CR/LF).
- R1-R4 are rare and mostly adversarial, but each is a guaranteed 300 s miss, and the guards cost nothing on normal
  patches.
- **W1 (cost): docstring-only edits** are not inert to the AST check. A module or class docstring edit in a sqlglot file
  goes to `import_time_change` (selector.py:379), which takes the coverage of the whole dialect family plus name tests,
  often hundreds of tests and sometimes slow ones. Docstrings never execute under pytest here (see C), except that
  annotator docstrings can feed `co_consts` in test_typing.
- **W2 (cost, minor):** a `#` comment-line edit to `pyproject.toml`, `setup.cfg` or `Makefile` falls through to `other` →
  all (selector.py:466). pytest reads none of these here (no `[tool.pytest]` or `[pytest]` section).

## (c) Proposed changes (against the current selector.py)

**C1. Read the patch without newline translation** (fixes R3 for `.py` automatically: the CR stays in the `+` text,
`apply_patch` keeps it, and `ast.parse` sees the code after it). At selector.py:627:
```diff
-            text = patch.read_text(encoding="utf-8", errors="replace")
+            text = patch.read_bytes().decode("utf-8", "replace")  # keep lone CRs: Python and the fixture loaders split on them
```
`parse_patch` already strips one trailing `\r` per line (selector.py:51-52), so CRLF patches still parse.

**C2. A text guard on the AST-identical path.** Add next to `sql_comment_only`:
```python
COOKIE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)", re.M)  # PEP 263
LOADER_BREAKS = "\r\x0b\x0c\x1c\x1d\x1e\x85  "  # str.splitlines breaks besides \n


def added_text(f: dict) -> list[str]:
    return [t for h in f["hunks"] for tag, t, _ in h["lines"] if tag == "+"]


def bytes_inert(old: str, new: str, f: dict) -> bool:
    """The import decodes the same bytes the same way, so an identical AST really is inert."""
    if old.startswith("﻿") != new.startswith("﻿"):
        return False
    head = lambda s: "\n".join(s.split("\n")[:2])
    if COOKIE.findall(head(old)) != COOKIE.findall(head(new)):
        return False
    return not any("�" in t or "\r" in t for t in added_text(f))
```
In `python_change` (selector.py:340):
```diff
         if old_tree is not None and new_tree is not None and ast.dump(old_tree) == ast.dump(new_tree):
-            return set(), "ast-identical"
+            if bytes_inert(old, new, f):
+                return set(), "ast-identical"
+            if rel.startswith("tests/") and rel in self.by_file:
+                return set(self.by_file[rel]), "ast-identical-unsafe-bytes"
+            return set(self.all), "ast-identical-unsafe-bytes"
```
(An undecodable or mis-declared sqlglot module breaks its importers, which in practice means nearly everything, so
all is the right fallback. For a `�` that was already in the old line, the pinned sources have none, so the check
cannot fire on them by accident.)

**C3. Tighten `sql_comment_only`** (selector.py:485-488) for R2 and R4:
```diff
     changed = [text for h in f["hunks"] for tag, text, _ in h["lines"] if tag in "+-"]
-    return bool(changed) and all(t == "" or t.startswith("--") for t in changed)
+    return bool(changed) and all(
+        (t == "" or t.startswith("--")) and "�" not in t and not any(c in t for c in LOADER_BREAKS)
+        for t in changed
+    )
```
If the guard trips, the edit falls through to fixture-stem, which is the normal fixture path.

**C4 (optional, cost W1). Docstring-insensitive comparison**, used only after the plain dump differs:
```python
def strip_docstrings(tree):
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body \
                and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant) \
                and isinstance(n.body[0].value.value, str) and len(n.body) > 1:
            n.body[0].value.value = ""
    return ast.dump(tree)
```
In `python_change`, after the identical check:
`if rel.startswith("sqlglot/") and not rel.startswith("sqlglot/typing/") and bytes_inert(old, new, f) and strip_docstrings(old_tree) == strip_docstrings(new_tree): return set(), "docstring-only"`.
Parse fresh trees first, or copy them, because `strip_docstrings` mutates. Exclusions: `sqlglot/typing/`, because
docstrings enter `co_consts`, which `_same_code` compares (tests/test_typing.py:21). `tests/` files are left alone
(already cheap). `len(n.body) > 1` makes sure a docstring that is the only statement still counts as a change. Re-check
before shipping that `tests.json` still has no `test_docs.py` entries; if pytest ever collects doctests, drop C4.

**C5 (optional, W2).** For `.toml`, `.cfg` and `.ini` at the repo root, when every changed line is empty or its
stripped text starts with `#`, return `[]` instead of `other`. Low frequency and low value; only do it if cheap.

## (d) Ranked recommendations
1. **C1 + C2** (patch bytes, cookie, BOM, U+FFFD and CR guard on the AST-identical path). These remove three
   guaranteed-miss shapes (R1-R3). Zero cost on normal comment patches, since the guard only adds a few string scans,
   and about 10 lines of code. High priority because a miss is 300 s.
2. **C3** (`.sql` comment rule matches `str.splitlines` and strict UTF-8). Closes R2 and R4 for fixtures. Trivial.
3. **Keep the AST-identical → `[]` rule.** The search found no line-number, traceback, warning-location, source-text
   or `# type:`/`# noqa` dependency in the tests or the runtime (section C), so widening comment edits to "some tests,
   just in case" would only add cost.
4. **C4** docstring-only → `[]` outside `sqlglot/typing/`. Medium cost win when a docstring edit hits a class or
   module body, since it avoids `import_time_change`. Small recall risk, and only if doctests start being collected.
5. **C5** config-comment edits → `[]`. Marginal.
