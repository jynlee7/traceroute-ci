# 19 - Comment-looking edits: can they break tests? (audit of selector.py at the pin)

Verdict: `[]` for comment-only Python commits is safe in general under the stated harness (pytest, Python 3.13).
I found no way for a comment-only `.py` edit to fail a test. There are 2 small hardening items and 2 cost-only
over-conservative spots. There is no saving to gain on the 3 sample comment commits (they are already `[]`).
Everything below was read from source text only. Nothing was run.

## 1. Harness facts that the argument rests on
- Failures are pytest-style (`README.md:26`). `sample/tests.json` has 1620 ids, none from `tests/test_docs.py` or
  `tests/test_integration_loader.py`.
- `tests/test_docs.py:9-26` runs doctests through unittest `load_tests`, which pytest ignores. The file defines no
  TestCase, so it collects 0 items. No `--doctest-modules` or `addopts` appears in `setup.cfg`, `pyproject.toml` or the
  Dockerfile (grep returned nothing). The Makefile (`make test` = `python -m unittest`) is the only path that would run
  doctests, and the scorer does not use it.
  **Assumption to keep:** if the hidden scorer ever ran `unittest`, docstring edits (31 modules contain `>>>`) could fail
  `test_docs`. `selector.py:461` already states this reasoning, and plan.md:127 and research/10 agree.
- Nothing in `sqlglot/` or `tests/` reads Python source line numbers, `__doc__`, `getsource` or `linecache`.
  Grep for `__doc__|getdoc|getsource|linecache|lineno|co_firstlineno|f_lineno|inspect.` finds only these:
  - `expressions/core.py:112`, which sets `cls.__doc__ = __doc__ or ""` (never read afterwards);
  - `tests/leakcheck.py:330`, a CLI script that is not collected;
  - `optimizer/optimizer.py:111` and `executor/env.py:101`, which use `inspect.signature` and `getfullargspec` (arguments, not docs);
  - `helper.py:153`, which uses `getmembers` for classes.
  So adding or removing lines cannot change behaviour.
- No test runs mypy, ruff, flake8 or subprocess (`grep mypy|ruff|flake8|subprocess tests/*.py` finds only `skipIf` text).
  `# noqa` and `# type:` are therefore inert at runtime.
  `ast.parse(..., type_comments=False)` (the default, and what `selector.py:446` uses) ignores them too.
  The only runtime-visible comment forms in Python are the coding cookie (lines 1-2), a shebang (not applicable) and
  `# doctest:` directives (inside docstrings, only run by unittest).

## 2. Hazard-by-hazard check against selector.py

| Hazard | Result | Where handled |
|---|---|---|
| `#` or `--` or `/* */` inside a Python string literal | The string constant changes, so `ast.dump` differs. It is routed as code (coverage window), never as a comment. For `tests/*.py` it goes to `test_file_change` (`selector.py:595-627`). | `selector.py:458` |
| Inserted comment inside a triple-quoted SQL string | Same: it is a string change. Not `[]`. | `selector.py:458` |
| `# noqa`, `# type: ignore`, `# fmt:`, `# pragma`, `# pylint` | AST identical and inert at runtime, so `[]`. Safe, because there is no mypy or lint test and mypyc is not used (the Dockerfile installs pure source, and the `sqlglotc` test mocks `mypycify`). | `selector.py:458-473` |
| Comment after a backslash continuation | SyntaxError, so everything runs (`syntax-error`, `selector.py:447-451`). Safe. | |
| Coding cookie added or removed in lines 1-3 | `bytes_inert` returns False, so all tests run. See hole H1 for the shifted-cookie case. | `selector.py:766-778` |
| `\r`, NUL or BOM in a changed line | `bytes_inert` returns False, so all tests run (or the test module's reach if the file is a test). Cost-only (C1 below). | same |
| Docstring edits (module, class, function) | `strip_docstrings` (`selector.py:781-788`) makes them `[]`. Safe under pytest because nothing reads `__doc__` (section 1). `sqlglot/typing/` is excluded, correctly: `tests/test_typing.py:11-19` compares `co_consts`, which contains a function's docstring. | `selector.py:459-466` |
| Docstrings used by `Func` metadata or `sql_names` | None exist. `sql_names` and `_sql_names` come from class attributes. `arg_types` is a dict. There is no docstring parsing anywhere in `sqlglot/` (grep). `class_sql_names` (`selector.py:679`) reads assignments, not docs. | n/a |
| `--` or blank lines in a `.sql` fixture | `sql_comment_only` (`selector.py:791-802`) gives `[]` only if every changed line is `""` or starts with `--` at column 0. This matches `tests/helpers.py:11-12` (`line and not line.startswith("--")`), applied via `_filter_comments` to every fixture read. | `selector.py:630` |
| Indented `--`, trailing `-- x` or `/* x */` in a fixture | NOT `[]`. These reach sqlglot, which preserves comments in its output, so the line goes through `fixture-stem`. Correct. | `selector.py:630-639` |
| `/* */` inside Python test strings | AST differs, so it goes through `test_file_change`. Correct. | |

### Why the fixture rule is exact, not approximate
1. All `.sql` files under `tests/` are read only through `load_sql_fixtures` and `load_sql_fixture_pairs`. Both call
   `_filter_comments` on the full text first (`helpers.py:34-52`). I grepped every `open(`, `read_text` and
   `FIXTURES_DIR` use in `tests/`. The others are `cts.json` and the executor's gz CSVs, which are never `.sql`.
2. Removing or adding a filtered line leaves the filtered text byte-identical. So `;` splitting, `# title:` meta
   parsing, `enumerate(..., start=1)` indexes (`test_optimizer.py:225`) and subTest ids are unchanged.
   This also holds when the `--` line sits inside a multi-line `/* */` or string in the fixture: the filter drops it
   in both versions.
3. `str.splitlines()` splits on more characters than diff lines do. `SPLITLINES_EXTRA` (`selector.py:763`) covers
   `\r \x0b \x0c \x1c-\x1e \x85 U+2028 U+2029`. This is correct and conservative.
4. A diff line is parsed inside a hunk by count (`selector.py:62`), so a removed `-- x` line (which prints as
   `--- x`) is not mistaken for a file header.
5. `\ No newline at end of file` cases produce a non-comment `-`/`+` pair, so they stay conservative.
Effect: tpc-ds and tpc-h `--` edits skip `test_execute_tpcds` (21.5 s). That is correct.

## 3. Holes found

### H1 (correctness, theoretical, ~0 s): a coding cookie shifted out of lines 1-2
`bytes_inert` (`selector.py:766`) checks the cookie regex only on changed lines with `old_no <= 3`.
Inserting a comment ABOVE an existing `# -*- coding: latin-1 -*-` leaves the cookie as an untouched context line,
which moves it to line 3, where Python ignores it. The decoding then differs, and the AST of the (utf-8-decoded)
string matches.
At the pin no `sqlglot/` or `tests/` file has a cookie. The `coding` grep hits are `encoding=` identifiers in
`parser.py` and `parsers/postgres.py`, plus a few tests. A hidden commit cannot create the situation unless it also
adds a cookie. This is a free fix:
```python
# selector.py, next to COOKIE
def cookie_head(text: str) -> tuple:
    """Which of the first two lines is a coding cookie (what Python's decoder sees)."""
    return tuple(bool(COOKIE.match(l)) for l in text.split("\n")[:2])
```
and in `python_change` where `same` is true (`selector.py:467`), change
`if not bytes_inert(f):` to
`if not bytes_inert(f) or cookie_head(old) != cookie_head(new):`.
Risk: none. Cost: 0 s on the sample (no cookies).

### H2 (cost-only): `bytes_inert` is all-or-nothing
Any `\r` in a changed line of a `sqlglot/` file makes all tests run (`bytes-suspect`, `selector.py:468-473`).
A comment-only edit in a CRLF file (or a file whose edit adds `\r`) would cost the whole suite.
Not safely prunable: with `\r`, the source's newline handling differs between `read_text` (universal newlines)
and the patch text. I recommend leaving it as is. At most, a test-file variant already exists (`bytes-suspect-test`).

### H3 (cost-only, rare): comment-only edits in non-Python config files run everything
`select` (`selector.py:663-672`) sends `pyproject.toml`, `setup.cfg`, `Makefile`, `.yml`, `.toml` and `.ini` to
`other`, which runs all tests. A diff where every changed line is blank or starts with `#` after leading whitespace,
in `Makefile`, `.gitignore` or `.github/**` (already `docs`), could be `[]`.
Caveat: `pyproject.toml` and `setup.cfg` can hold pytest config, and TOML multi-line strings can contain `#`-lines as
data, so keep those files conservative. Only `Makefile`, `.gitignore`, `MANIFEST.in` and `*.yml` outside `tests/`
would be safe. Expected frequency in hidden commits is ~0, so I rate this not worth doing.

### H4 (cost-only, rare): edits to zero-test, unimported test-tree files run everything
`tests/test_docs.py` and `tests/test_integration_loader.py` have no collected tests and no importers.
`test_file_change` returns `self.all` when `reach is None` (`selector.py:600-603`).
Under pytest a break in those files fails nothing (zero tests, and the import error is not attributable to a test).
`tests/leakcheck.py` and `tests/gen_fixtures.py` are not collected at all (not `test_*.py`).
A diff that only touches these could return `set()`. The saving is large for such a commit (~all tests), but the
frequency is likely 0 in the hidden set. The rule must stay derived rather than a name list: "file is in the test tree,
has no collected tests and no importers in `test_rdeps`, and its name does not match `conftest.py`".
Do not do this for `tests/helpers.py` (it has importers) or any `__init__.py` under `tests/`: `__init__.py` makes the
package importable, so package-level breaks can fail every test below it.
Risk: low-medium (an `__init__.py` or `conftest`-like surprise). Put it on the optional list.

## 4. Things I checked that are NOT holes (avoid chasing them)
- `strip_docstrings` also strips class and function docstrings in `tests/`. Fine: unittest `shortDescription` only
  affects the runner's display text, not pytest ids.
- Docstring edit in a class with `__init_subclass__` (`core.py:112`): `__doc__` changes from text to `""`, but nothing
  reads it.
- Changing implicit string concatenation, number spelling (`0x10` vs `16`), quote style or `u""` prefixes yields an
  identical AST and truly identical runtime. It is correct for these to be `[]`.
- Comments inside multi-line f-string expressions (3.12+ syntax): AST identical and inert.
- Trailing-newline-only changes: `apply_patch` + `ast.parse` give an identical AST, which is inert.
- `ast.dump` omits line numbers, and nothing at runtime depends on source lines, so inserted lines are inert.
  The only line-keyed data is the coverage map, which is consulted only for non-identical ASTs.
- `# type:` comments: only read if `type_comments=True` (nothing does that) or by mypy/mypyc (not run).
  `sqlglotc/` is routed to `test_sqlglotc` tests only (`selector.py:656-660`); the test mocks `mypycify`.
- Python 3.13 (Dockerfile:2) raises `SyntaxError` (not `ValueError`) for NUL in source, so `except SyntaxError`
  (`selector.py:447`) is sufficient. `bytes_inert` also rejects NUL.
- `sqlglot/typing/` comment-only edits: `ast-identical` is fine. `co_code`, `co_names`, `co_varnames` and `co_consts`
  do not include comments or line numbers (3.13 keeps the line table separate from `co_code`).

## 5. Patch shapes that WOULD be a miss if someone widened the comment rules
These are all currently handled, but they are what to avoid when tuning:
1. "Only `+` lines starting with `#` -> `[]`" without an AST check. A `#` line inserted inside a triple-quoted SQL
   string (test or `sqlglot/`) changes the string.
2. "Any `.sql` diff where lines contain `--` -> `[]`". Only column-0 `--` lines are filtered; ` -- x` and `a -- x`
   change parsed SQL, which preserves comments.
3. Treating docstring edits as inert inside `sqlglot/typing/` (the `co_consts` comparison).
4. Treating docstring edits as inert if the scorer is ever switched to `python -m unittest` or `--doctest-modules`.
5. Dropping the `bytes_inert` check for coding cookies, BOM or `\r`.

## 6. Ranked recommendations
| # | Change | Seconds saved on sample | Risk |
|---|---|---|---|
| 1 | Keep the comment/docstring/fixture-comment logic as is (it is correct); no change needed for the 3 `[]` commits. | 0 (already 0) | none |
| 2 | H1: add `cookie_head` comparison (7 lines). | 0 | none (strictly more conservative) |
| 3 | H4: `set()` for test-tree `.py` files with 0 collected tests, no importers, not conftest or `__init__`. | 0 on sample; large per hit | low-medium |
| 4 | H3: comment-only `Makefile` / `.gitignore` / `MANIFEST.in` / top-level `*.yml` -> `[]`. | 0 | low |
| 5 | Add one defensive assert in `build_index.py` or at selector startup: `tests.json` has no `tests/test_docs.py::` ids; if any appear (scorer collects doctests), disable `docstring-only` for non-`typing/` files too. | 0 | none |

Recommendation 5 is a guard for the one assumption in section 1. A snippet:
```python
# in Selector.__init__, after self.all is built
self.docs_collected = any(t["id"].startswith("tests/test_docs.py") for t in tests)
# in python_change: gate the docstring strip
if not same and not rel.startswith("sqlglot/typing/") and not self.docs_collected:
    strip_docstrings(old_tree); strip_docstrings(new_tree)
```
(`tests` entries are dicts keyed by id; check the exact field name in `tests.json` before using it.)

Summary: no hole lets a comment-only `.py` edit fail a pytest test at the pin. The fixture `--` rule matches
`helpers.py` exactly. Only theoretical or cost-only gaps (H1-H4) remain.
