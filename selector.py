"""Blast Radius selector.

For each patch, picks the tests that can see the change from a coverage map of
sqlglot at the pinned commit, then splits them over the number of workers that
makes the commit cheapest. Logs go to stderr; stdout holds only answer lines.
"""

import ast
import bisect
import gzip
import json
import os
import re
import sys
from pathlib import Path

DATA = Path(os.environ.get("DATA", "/data"))
APP = Path(os.environ.get("APP", "/app"))
SRC = Path(os.environ.get("SRC", APP / "src"))  # sqlglot checkout at the pinned commit
MAP = Path(os.environ.get("MAP", APP / "maps/impact.json.gz"))
INDEX = Path(os.environ.get("INDEX", APP / "maps/index.json"))  # built by tools/build_index.py

# A dialect lives in one file per directory, all named after it.
DIALECT_DIRS = ("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/", "sqlglot/typing/", "sqlglot/tokenizers/")
TYPING_TEST = "tests/test_typing.py"  # reads every sqlglot/typing/*.py file directly

WORKER_MS = 2000
MAX_WORKERS = 8
# How many lines around a changed line (inside the same function) count as touched.
WINDOW = int(os.environ.get("WINDOW", "2"))
# Tests at least this slow are kept by the function rule only when they ran the changed line itself.
SLOW_MS = 5000
DOC_SUFFIXES = {".md", ".rst", ".txt", ".png", ".svg", ".html", ".css", ".ipynb"}

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def log(*args) -> None:
    print(*args, file=sys.stderr)


# ---------------------------------------------------------------- patches


def parse_patch(text: str) -> list[dict]:
    """Files of a unified diff, each with old/new paths and hunks of (tag, text, old_lineno)."""
    files: list[dict] = []
    cur = None
    old_no = 0
    left_old = left_new = 0  # lines still expected in the current hunk
    for line in text.split("\n"):
        if line.endswith("\r"):
            line = line[:-1]
        if line.startswith("diff --git "):
            git_old, git_new = git_paths(line[len("diff --git "):])
            cur = {"old": None, "new": None, "hunks": [], "binary": False, "rename": False,
                   "created": False, "deleted": False, "seen_minus": False, "seen_plus": False,
                   "git_old": git_old, "git_new": git_new}
            files.append(cur)
            left_old = left_new = 0
            continue
        if cur is None:
            continue
        in_hunk = left_old > 0 or left_new > 0
        if in_hunk and line[:1] in (" ", "-", "+", ""):
            tag = line[:1] or " "  # an empty line is context whose trailing space was stripped
            cur["hunks"][-1]["lines"].append((tag, line[1:], old_no))
            if tag != "+":
                old_no += 1
                left_old -= 1
            if tag != "-":
                left_new -= 1
            continue
        if line.startswith("\\"):
            continue  # "\ No newline at end of file"
        if m := HUNK.match(line):
            start = int(m.group(1))
            left_old = int(m.group(2)) if m.group(2) is not None else 1
            left_new = int(m.group(4)) if m.group(4) is not None else 1
            # With no old lines, the header names the line *before* the insertion.
            old_no = start + 1 if left_old == 0 else start
            cur["hunks"].append({"old_start": old_no, "lines": []})
        elif cur["hunks"]:
            continue  # trailers after the last hunk (e.g. format-patch "-- " signature)
        elif line.startswith("--- "):
            cur["old"] = header_path(line[4:], "a/")
            cur["seen_minus"] = True
        elif line.startswith("+++ "):
            cur["new"] = header_path(line[4:], "b/")
            cur["seen_plus"] = True
        elif line.startswith(("Binary files", "GIT binary patch")):
            cur["binary"] = True
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            cur["rename"] = True
        elif line.startswith("new file mode"):
            cur["created"] = True
        elif line.startswith("deleted file mode"):
            cur["deleted"] = True
    for f in files:
        # Header-only diffs (binary, pure rename, mode change) have no ---/+++ lines.
        if not f["seen_minus"]:
            f["old"] = None if f["created"] else f["git_old"]
        if not f["seen_plus"]:
            f["new"] = None if f["deleted"] else f["git_new"]
        f["path"] = f["new"] or f["old"] or ""
    return files


def unquote_path(p: str) -> str:
    """git quotes paths with unusual characters: "a/x\\ty" with C escapes."""
    if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
        try:
            return p[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape").encode("latin-1").decode("utf-8", "replace")
        except Exception:
            return p[1:-1]
    return p


def header_path(p: str, prefix: str) -> str | None:
    p = unquote_path(p.split("\t")[0].rstrip())
    if p == "/dev/null":
        return None
    return p[len(prefix):] if p.startswith(prefix) else p


def git_paths(rest: str) -> tuple[str | None, str | None]:
    """Old and new paths from the `diff --git a/X b/Y` line."""
    rest = rest.strip()
    if rest.startswith('"'):
        end = rest.find('"', 1)
        a, b = rest[: end + 1], rest[end + 1:].strip()
    else:
        i = rest.rfind(" b/")
        if i < 0:
            return None, None
        a, b = rest[:i], rest[i + 1:]
    a, b = unquote_path(a), unquote_path(b)
    return (a[2:] if a.startswith("a/") else a), (b[2:] if b.startswith("b/") else b)


def touched_old_lines(f: dict) -> set[int]:
    """Old line numbers that were removed, plus both neighbours of every pure insertion.

    Added lines that directly replace removed lines are located by those removed lines.
    """
    out: set[int] = set()
    for h in f["hunks"]:
        replacing = False  # inside a run of "+" lines that follows "-" lines
        for tag, _text, old_no in h["lines"]:
            if tag == "-":
                out.add(old_no)
                replacing = True
            elif tag == "+":
                if not replacing:
                    out.add(old_no)  # the line that follows the insertion
                    out.add(max(1, old_no - 1))  # the line before it
            else:
                replacing = False
    return out


def apply_patch(old: str, f: dict) -> str | None:
    """The new text of one file, or None if the hunks don't apply."""
    src = old.split("\n")
    out: list[str] = []
    pos = 0  # 0-based index into src
    for h in f["hunks"]:
        start = max(h["old_start"] - 1, 0)  # old_start is the first old line the hunk touches
        if start < pos:
            return None
        out.extend(src[pos:start])
        pos = start
        for tag, text, _ in h["lines"]:
            if tag == " ":
                if pos >= len(src) or src[pos] != text:
                    return None
                out.append(text)
                pos += 1
            elif tag == "-":
                if pos >= len(src) or src[pos] != text:
                    return None
                pos += 1
            else:
                out.append(text)
    out.extend(src[pos:])
    return "\n".join(out)


# ---------------------------------------------------------------- selection


class Selector:
    def __init__(self, tests: list[dict]):
        self.tests = tests
        self.by_id = {t["id"]: t["number"] for t in tests}
        self.all = {t["number"] for t in tests}
        self.slow = frozenset(t["number"] for t in tests if round(t["seconds"] * 1000) >= SLOW_MS)
        self.by_file: dict[str, set[int]] = {}
        for t in tests:
            self.by_file.setdefault(t["file"], set()).add(t["number"])
        with gzip.open(MAP, "rt") as fh:
            m = json.load(fh)
        # Map test indexes in the map onto this run's numbers, by id.
        number_of = [self.by_id.get(tid) for tid in m["tests"]]
        self._number_of = number_of
        self._raw_sets = m["sets"]
        self._sets: dict[int, frozenset] = {}  # built on first use: a run touches few of them
        self.files = m["files"]
        self.strings = {number_of[int(k)]: v for k, v in m["test_strings"].items() if number_of[int(k)] is not None}
        self.string_sets = {n: frozenset(v) for n, v in self.strings.items()}
        self.string_heads = {n: frozenset(s.split(",", 1)[0].strip() for s in v if "," in s and "=" in s)
                             for n, v in self.strings.items()}
        self._src_cache: dict[str, str | None] = {}
        try:
            self.index = json.loads(INDEX.read_text())
        except Exception as e:
            log(f"no index ({type(e).__name__}); import-time rules use file coverage only")
            self.index = {}

    def source(self, rel: str) -> str | None:
        if rel not in self._src_cache:
            p = SRC / rel
            self._src_cache[rel] = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None
        return self._src_cache[rel]

    # -- helpers

    def test_set(self, sid: int) -> frozenset:
        s = self._sets.get(sid)
        if s is None:
            num = self._number_of
            s = self._sets[sid] = frozenset(n for n in (num[i] for i in self._raw_sets[sid]) if n is not None)
        return s

    def cov(self, rel: str, lines) -> set[int]:
        table = self.files.get(rel, {}).get("lines", {})
        out: set[int] = set()
        for ln in lines:
            sid = table.get(str(ln))
            if sid is not None:
                out |= self.test_set(sid)
        return out

    def file_cov(self, rel: str) -> set[int]:
        table = self.files.get(rel, {}).get("lines", {})
        out: set[int] = set()
        for sid in set(table.values()):
            out |= self.test_set(sid)
        return out

    def innermost(self, rel: str, line: int):
        """(function_range, class_range) innermost containing line, either may be None."""
        fn = cls = None
        for start, end, kind in self.files.get(rel, {}).get("ranges", []):
            if start <= line <= end:
                if kind == 0:
                    fn = (start, end)
                else:
                    cls = (start, end)
        return fn, cls

    def name_tests(self, words: set[str]) -> set[int]:
        """Tests in test files named after any word, e.g. snowflake -> tests/dialects/test_snowflake.py,
        plus tests holding the word as a whole string, e.g. dialect="snowflake" or write={"snowflake": ...}."""
        out: set[int] = set()
        for w in words:
            for f, nums in self.by_file.items():
                if Path(f).stem == f"test_{w}":
                    out |= nums
        if words:
            out |= {n for n, s in self.string_sets.items() if not s.isdisjoint(words)}
            # Dialect strings with settings: "duckdb, version=1.0".
            out |= {n for n, heads in self.string_heads.items() if not heads.isdisjoint(words)}
            # Fixture-driven tests name their dialect in "# dialect: x" headers, not in Python.
            fixture_dialects = self.index.get("fixture_dialects", {})
            for w in words:
                for fx in fixture_dialects.get(w, []):
                    out |= self.fixture_tests(fx)
        return out

    def dialect_family(self, rel: str) -> set[str]:
        """Every file of the dialect `rel` belongs to, and of dialects that import it (subclasses)."""
        if not rel.startswith(DIALECT_DIRS) or Path(rel).stem in ("__init__", "base"):
            return {rel}
        rdeps = self.index.get("rdeps", {})
        family: set[str] = set()
        todo = [rel]
        while todo:
            cur = todo.pop()
            if cur in family:
                continue
            family.add(cur)
            stem = Path(cur).stem
            todo.extend(d + f"{stem}.py" for d in DIALECT_DIRS if f"{d}{stem}.py" in self.files)
            todo.extend(r for r in rdeps.get(cur, []) if r.startswith(DIALECT_DIRS) and Path(r).stem not in ("__init__", "base"))
        return family

    def top_class(self, rel: str, line: int) -> str | None:
        for start, end, name in self.index.get("toplevel", {}).get(rel, []):
            if start <= line <= end:
                return name
        return None

    def class_tests(self, name: str) -> set[int]:
        """Coverage of every line naming the class or a subclass, and of their `<lower>_sql` methods."""
        subs, refs = self.index.get("subs", {}), self.index.get("refs", {})
        names, todo = set(), [name]
        while todo:
            n = todo.pop()
            if n not in names:
                names.add(n)
                todo.extend(subs.get(n, []))
            if len(names) > 400:
                return set(self.all)  # a base of most classes: everything depends on it
        out: set[int] = set()
        for n in names:
            for rel, start, end in refs.get(n, []):
                out |= self.cov(rel, range(start, end + 1))
        return out

    def body_start(self, rel: str, fn: tuple[int, int]) -> int:
        """Last line of a function's header: the first line of its range that coverage records
        for a call, minus one (decorators and a multi-line signature run at definition time)."""
        table = self.files.get(rel, {}).get("lines", {})
        for x in range(fn[0] + 1, fn[1] + 1):
            sid = table.get(str(x))
            if sid is not None and self.test_set(sid):
                return x - 1
        return fn[1]  # nothing ran in a call: treat the whole function as header

    def in_import_fn(self, rel: str, line: int) -> bool:
        """Is the line in a function that runs at import, outside any def or lambda nested in it?"""
        for start, end, nested in self.index.get("import_fns", {}).get(rel, []):
            if start <= line <= end and not any(a <= line <= b for a, b in nested):
                return True
        return False

    def fixture_tests(self, fixture_rel: str) -> set[int]:
        """Tests whose strings name the fixture file (same matching as fixture_change)."""
        stem = Path(fixture_rel).stem
        memo = self.__dict__.setdefault("_fx_memo", {})
        if stem not in memo:
            memo[stem] = {n for n, strings in self.strings.items() if any(stem in s for s in strings)}
        return set(memo[stem])

    def lambda_range(self, rel: str, line: int):
        best = None
        for start, end in self.index.get("lambdas", {}).get(rel, []):
            if start <= line <= end and (best is None or end - start < best[1] - best[0]):
                best = (start, end)
        return best

    def sql_name_tests(self, cls: str, extra_names: set[str]) -> set[int] | None:
        """Tests calling the SQL names of cls or its subclasses. None: can't tell, run everything."""
        calls, call_tests = self.index.get("calls"), self.index.get("call_tests")
        if calls is None or call_tests is None:
            return None
        subs, todo, seen = self.index.get("subs", {}), [cls], set()
        while todo:
            n = todo.pop()
            if n in seen:
                continue
            seen.add(n)
            todo.extend(subs.get(n, []))
            if len(seen) > 200:
                return None  # a base class of most expressions
        words = set(extra_names)
        for n in seen:
            words |= set(calls.get(n, ()))
        out: set[int] = set()
        for w in words:
            tids = call_tests.get(w, ())
            if "*" in tids:
                return None
            out |= {self.by_id[t] for t in tids if t in self.by_id}
        return out

    def import_time_change(self, rel: str, line: int, extra_names: set[str] = frozenset()) -> set[int]:
        # Same answer for every line of one top-level class (or module level): memoise.
        key = (rel, self.top_class(rel, line), frozenset(extra_names))
        memo = self.__dict__.setdefault("_itc_memo", {})
        if key not in memo:
            memo[key] = self._import_time_change(rel, line, extra_names)
        return set(memo[key])

    def _import_time_change(self, rel: str, line: int, extra_names: set[str] = frozenset()) -> set[int]:
        """A change to a class body or module level: it runs at import and is read from anywhere."""
        family = self.dialect_family(rel)
        hit: set[int] = set()
        for f in family:
            hit |= self.file_cov(f)
        if rel.startswith(DIALECT_DIRS):
            # Only dialect module names are meaningful words; "string" or "temporal" would match
            # unrelated tests and hide an empty real signal from the run-everything fallback.
            words = {Path(f).stem for f in family} - {"__init__", "base"}
            hit |= self.name_tests(words)
        cls = self.top_class(rel, line)
        if cls:
            hit |= self.class_tests(cls)
        if cls and rel.startswith("sqlglot/expressions/"):
            by_name = self.sql_name_tests(cls, extra_names)
            if by_name is None:
                return set(self.all)
            hit |= by_name
        if rel.startswith(DIALECT_DIRS) or rel == "sqlglot/dialects/dialect.py":
            # Tests looping over every dialect name none, and see import-time dialect state.
            # (test_generator_memory included: it loads every dialect, so an edit that breaks a
            # dialect module's import fails it too.)
            for tid in self.index.get("all_dialect_tests", []):
                n = self.by_id.get(tid)
                if n is not None:
                    hit.add(n)
        return hit

    def test_module_reach(self, rel: str) -> set[int] | None:
        """Tests in a test module and in every test module importing it, transitively.

        None when nothing is known about the module (no tests and no importers): run everything.
        """
        rdeps = self.index.get("test_rdeps")
        if rdeps is None:
            return None
        seen, todo = set(), [rel]
        while todo:
            cur = todo.pop()
            if cur in seen:
                continue
            seen.add(cur)
            todo.extend(rdeps.get(cur, []))
        out: set[int] = set()
        for m in seen:
            out |= self.by_file.get(m, set())
        return out or None

    # -- per kind of file

    def python_change(self, f: dict) -> tuple[set[int], str]:
        rel = f["path"]
        old = self.source(f["old"]) if f["old"] else ""
        if old is None:
            return set(self.all), "unknown-file"
        new = apply_patch(old, f) if f["new"] else ""
        if new is None:
            return set(self.all), "patch-did-not-apply"
        try:
            new_tree = ast.parse(new) if new else None
        except SyntaxError:
            if rel.startswith("tests/"):
                reach = self.test_module_reach(rel)
                if reach is not None:
                    return reach, "syntax-error-test"
            return set(self.all), "syntax-error"
        try:
            old_tree = ast.parse(old) if old else None
        except SyntaxError:
            return set(self.all), "old-syntax-error"
        if old_tree is not None and new_tree is not None:
            same = ast.dump(old_tree) == ast.dump(new_tree)
            how = "ast-identical"
            if not same and not rel.startswith("sqlglot/typing/"):
                # Docstring-only edits: pytest collects no doctests here and nothing reads
                # docstrings (typing/ excluded: test_typing compares code constants).
                strip_docstrings(old_tree)
                strip_docstrings(new_tree)
                same = ast.dump(old_tree) == ast.dump(new_tree)
                how = "docstring-only"
            if same:
                if not bytes_inert(f):
                    # What Python sees can differ from what the AST saw: coding cookie, BOM,
                    # undecodable bytes, stray carriage returns.
                    if rel.startswith("tests/"):
                        reach = self.test_module_reach(rel)
                        if reach is not None:
                            return reach, "bytes-suspect-test"
                    return set(self.all), "bytes-suspect"
                return set(), how
        if rel.startswith("tests/"):
            return self.test_file_change(f, old), "test-file"
        if not rel.startswith("sqlglot/"):
            return set(self.all), "other-python"
        return self.sqlglot_change(f, old_tree, new_tree)

    def sqlglot_change(self, f, old_tree, new_tree) -> tuple[set[int], str]:
        rel = f["path"]
        if f["old"] is None or f["new"] is None:
            return set(self.all), "file-added-or-removed"
        changed = changed_statement_lines(old_tree, new_tree, touched_old_lines(f))
        removed = {old_no for h in f["hunks"] for tag, _t, old_no in h["lines"] if tag == "-"}
        prunable = same_indent_replacements(f)
        # SQL names whose owner changed (_sql_names edits): tests calling the old or new names.
        renamed: set[str] = set()
        if rel.startswith("sqlglot/expressions/"):
            old_names, new_names_ = class_sql_names(old_tree), class_sql_names(new_tree)
            for c in old_names.keys() | new_names_.keys():
                if old_names.get(c) != new_names_.get(c):
                    renamed |= old_names.get(c, set()) | new_names_.get(c, set())
        out: set[int] = set()
        reasons = []
        for ln in sorted(changed):
            fn, cls = self.innermost(rel, ln)
            if fn and self.in_import_fn(rel, ln):
                # Runs at import or class creation (hooks, metaclasses, helpers they call):
                # coverage only credits whichever test imported first.
                hit = self.import_time_change(rel, ln) | self.cov(rel, range(fn[0], fn[1] + 1))
                if not hit:
                    return set(self.all), "import-fn-unmapped"
                out |= hit
                reasons.append("import-fn")
            elif fn:
                lo, hi = max(fn[0], ln - WINDOW), min(fn[1], ln + WINDOW)
                hit = self.cov(rel, range(lo, hi + 1))
                if hit & self.slow and ln in prunable and ln > self.body_start(rel, fn):
                    # A slow test (>= 5 s) is kept only if it ran the changed line itself. Only for
                    # same-indent line replacements on a line coverage records: continuation lines,
                    # insertions and code moved between blocks keep the whole window.
                    table = self.files.get(rel, {}).get("lines", {})
                    if str(ln) in table and self.test_set(table[str(ln)]):
                        exact = self.cov(rel, [ln])
                        hit = (hit - self.slow) | (exact & self.slow)
                if not hit:
                    # Nothing near the change ran: take the whole function, then the class.
                    hit = self.cov(rel, range(fn[0], fn[1] + 1))
                if not hit and cls:
                    hit = self.cov(rel, range(cls[0], cls[1] + 1))
                if not hit:
                    table = self.files.get(rel, {}).get("lines", {})
                    if any(str(x) in table for x in range(fn[0], fn[1] + 1)):
                        # The function ran, but only at import or collection (no test context):
                        # treat it like a module-level edit; if nothing claims it, run everything.
                        hit = self.import_time_change(rel, ln) or set(self.all)
                        reasons.append("fn-import-only")
                out |= hit
                reasons.append("fn")
            else:
                lam = self.lambda_range(rel, ln)
                hit = self.cov(rel, range(lam[0], lam[1] + 1)) if lam else set()
                if hit and ln in removed and ln != lam[0]:
                    # An edited line inside a lambda's body (class tables): the body runs only
                    # when called, and coverage sees that.
                    out |= hit
                    reasons.append("lambda")
                    continue
                # Insertions next to a lambda, or edits on its first line (where the table key
                # sits), can add or rename an entry: keep the lambda's callers and also take
                # the import-time rule below.
                out |= hit
                # Class body or module level: runs at import, consumed anywhere the module is used.
                if rel.endswith("__init__.py"):
                    # Package init: re-exports and lazy dialect loading reach everything.
                    return set(self.all), "package-init"
                hit = self.import_time_change(rel, ln, renamed)
                if not hit:
                    return set(self.all), "module-level-unmapped"
                out |= hit
                reasons.append("module")
        lost = module_bound(old_tree) - module_bound(new_tree)
        if lost:
            # Another sqlglot module importing it, or naming the class (exp.Foo in a table), fails
            # at import, and `import sqlglot` loads nearly everything: run everything.
            internal = self.index.get("internal_names", {})
            refs = self.index.get("refs", {})
            defined = {s.name for s in old_tree.body if isinstance(s, ast.ClassDef)}
            for n in lost:
                named_elsewhere = n in defined and any(r[0] != rel for r in refs.get(n, []))
                if internal.get(f"{rel}:{n}") or named_elsewhere:
                    return set(self.all), "sqlglot:lost-internal-name"
            # A removed or renamed module-level name that a test module imports makes that
            # module (and its importers) fail at collection. Keyed by name, so re-exports count.
            users = {m for n in lost for m in self.index.get("sqlglot_names", {}).get(n, [])}
            for m in users:
                out |= self.test_module_reach(m) or set(self.by_file.get(m, set()))
            if users:
                reasons.append("lost-import")
        new_names = def_names(new_tree) - def_names(old_tree)
        if new_names:
            # A new or renamed method can override a parent's or be found by name
            # (generators look up `<lower expression>_sql`): take every caller of that name.
            out |= self.new_method_tests(new_names)
            reasons.append("new-def")
        if rel.startswith("sqlglot/typing/"):
            out |= self.by_file.get(TYPING_TEST, set())
        return out, "sqlglot:" + ",".join(sorted(set(reasons)))

    def new_method_tests(self, names: set[tuple[str, str]]) -> set[int]:
        fns, refs = self.index.get("fns", {}), self.index.get("refs", {})
        lower_class = {n.lower(): n for n in refs}
        out: set[int] = set()
        for _cls, name in names:
            for rel, start, end in fns.get(name, []):
                out |= self.cov(rel, range(start, end + 1))
            if name.endswith("_sql") and name[:-4] in lower_class:
                out |= self.class_tests(lower_class[name[:-4]])
        return out

    def test_file_change(self, f: dict, old: str) -> set[int]:
        rel = f["path"]
        reach = self.test_module_reach(rel)
        if rel not in self.by_file:
            # A helper module (tests/helpers.py...): every test module importing it. A conftest
            # or anything unknown: run everything.
            if rel.endswith("conftest.py") or reach is None:
                return set(self.all)
            return reach
        tree = ast.parse(old)
        picked: set[int] = set()
        whole = False
        lines = touched_old_lines(f)
        for ln in lines:
            hit = None
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.lineno <= ln <= node.end_lineno and node.name.startswith("test"):
                    hit = f"{rel}::{node.name}"
                elif isinstance(node, ast.ClassDef) and node.lineno <= ln <= node.end_lineno:
                    for item in node.body:
                        start = min([item.lineno] + [d.lineno for d in getattr(item, "decorator_list", [])])
                        if isinstance(item, ast.FunctionDef) and start <= ln <= item.end_lineno and item.name.startswith("test"):
                            hit = f"{rel}::{node.name}::{item.name}"
            if hit and hit in self.by_id:
                picked.add(self.by_id[hit])
            else:
                whole = True
        if whole:
            # Outside a test method (helpers, base classes like test_dialect.Validator, imports,
            # setUp): this file and every test file importing it.
            return reach if reach is not None else set(self.all)
        return picked

    def fixture_change(self, f: dict) -> tuple[set[int], str]:
        path = Path(f["path"])
        if path.suffix == ".sql" and f["old"] and f["new"] and sql_comment_only(f):
            # tests/helpers.py's fixture loaders drop empty lines and lines starting with "--".
            return set(), "fixture-comment"
        stem, parent = path.stem, path.parent.name
        hits = {n for n, strings in self.strings.items() if any(stem in s for s in strings)}
        if hits:
            return hits, "fixture-stem"
        hits = {n for n, strings in self.strings.items() if any(parent in s for s in strings)}
        if hits:
            return hits, "fixture-dir"
        return set(self.all), "fixture-unmapped"

    def select(self, files: list[dict]) -> tuple[set[int], list[str]]:
        out: set[int] = set()
        why = []
        for f in files:
            rel = f["path"]
            suffix = Path(rel).suffix
            code_paths = ("sqlglot/", "tests/", "sqlglotc/")
            touches_code = any((p or "").startswith(code_paths) for p in (f["old"], f["new"], rel))
            if not rel:
                s, r = set(self.all), "unknown-path"
            elif f["binary"]:
                s, r = (set(self.all), "binary") if touches_code else (set(), "binary-other")
            elif f["rename"]:
                # Moving a module or fixture breaks whatever imports or reads it by its old name.
                s, r = (set(self.all), "rename") if touches_code else (set(), "rename-other")
            elif not f["hunks"] and not f["created"] and not f["deleted"]:
                s, r = set(), "mode-only"  # pytest ignores file modes
            elif rel.startswith("sqlglotc/"):
                # Only tests/test_sqlglotc.py runs sqlglotc/ (it loads sqlglotc/setup.py); the
                # suite runs pure Python sqlglot.
                s, r = set(self.by_file.get("tests/test_sqlglotc.py", set())) or set(self.all), "sqlglotc"
            elif suffix == ".py":
                s, r = self.python_change(f)
            elif rel.startswith("tests/fixtures/") or (rel.startswith("tests/") and suffix in (".sql", ".json", ".csv", ".txt")):
                s, r = self.fixture_change(f)
            elif suffix in DOC_SUFFIXES or rel.startswith(("docs/", ".github/")):
                s, r = set(), "docs"
            else:
                s, r = set(self.all), "other"
            out |= s
            why.append(f"{rel}:{r}:{len(s)}")
        return out, why


CAMEL = re.compile(r"(?<!^)(?=[A-Z])")  # same as sqlglot.helper.CAMEL_CASE_PATTERN


def class_sql_names(tree) -> dict[str, set[str]]:
    """Top-level class -> lower-case SQL names (snake case default, class name, _sql_names)."""
    out: dict[str, set[str]] = {}
    if tree is None:
        return out
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef):
            continue
        names = {CAMEL.sub("_", cls.name).lower(), cls.name.lower()}
        for st in cls.body:
            targets = st.targets if isinstance(st, ast.Assign) else [st.target] if isinstance(st, ast.AnnAssign) else []
            if any(getattr(t, "id", None) == "_sql_names" for t in targets) and st.value is not None:
                names |= {e.value.lower() for e in ast.walk(st.value)
                          if isinstance(e, ast.Constant) and isinstance(e.value, str)}
        out[cls.name] = names
    return out


def same_indent_replacements(f: dict) -> set[int]:
    """Old lines replaced one-for-one by a line with the same indentation (no moves, no inserts)."""
    out: set[int] = set()
    for h in f["hunks"]:
        lines = h["lines"]
        i = 0
        while i < len(lines):
            if lines[i][0] != "-":
                i += 1
                continue
            minus = []
            while i < len(lines) and lines[i][0] == "-":
                minus.append(lines[i]); i += 1
            plus = []
            while i < len(lines) and lines[i][0] == "+":
                plus.append(lines[i]); i += 1
            if len(minus) != len(plus):
                continue
            ind = lambda s: len(s) - len(s.lstrip())
            if all(ind(m[1]) == ind(p[1]) and m[1].strip() and p[1].strip() for m, p in zip(minus, plus)):
                out.update(m[2] for m in minus)
    return out


def module_bound(tree) -> set[str]:
    """Names bound at a module's top level (defs, classes, assignments, imports)."""
    out: set[str] = set()
    if tree is None:
        return out
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(stmt.name)
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            out.update((a.asname or a.name).split(".")[0] for a in stmt.names)
        else:
            targets = stmt.targets if isinstance(stmt, ast.Assign) else \
                [stmt.target] if isinstance(stmt, (ast.AnnAssign, ast.AugAssign)) else []
            for t in targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        out.add(n.id)
    return out


def def_names(tree) -> set[tuple[str, str]]:
    """(enclosing class, function name) of every def, at any depth."""
    out: set[tuple[str, str]] = set()
    if tree is None:
        return out

    def visit(node, cls: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, child.name)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.add((cls, child.name))
                visit(child, cls)
            else:
                visit(child, cls)

    visit(tree, "")
    return out


COOKIE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+")
# str.splitlines (used by the fixture loaders) also breaks on these, which a diff line doesn't.
SPLITLINES_EXTRA = ("\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " ")


def bytes_inert(f: dict) -> bool:
    """No byte-level effect that an AST comparison can't see."""
    for h in f["hunks"]:
        for tag, text, old_no in h["lines"]:
            if tag not in "+-":
                continue
            if "�" in text or "\r" in text or "\x00" in text:
                return False
            if text.startswith("﻿"):
                return False
            if old_no <= 3 and COOKIE.match(text):
                return False
    return True


def strip_docstrings(tree) -> None:
    """Drop module, class and function docstrings in place (a Pass keeps bodies non-empty)."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]


def sql_comment_only(f: dict) -> bool:
    """Every removed and added line is empty or starts with "--" (what the fixture loaders skip)."""
    changed = [text for h in f["hunks"] for tag, text, _ in h["lines"] if tag in "+-"]
    if not changed:
        return False
    for t in changed:
        if "�" in t or any(c in t for c in SPLITLINES_EXTRA):
            return False  # the loader would split or fail to decode this line differently
        if not (t == "" or t.startswith("--")):
            return False
    return True


def changed_statement_lines(old_tree, new_tree, touched: set[int]) -> set[int]:
    """Touched old lines, widened to the first line of every statement that spans them.

    Coverage records a multi-line statement on its first line only.
    """
    out = set(touched)
    if old_tree is None:
        return out
    for node in ast.walk(old_tree):
        # Only simple statements: a compound one's header (def, class, if, for...) is not
        # where its body's lines are recorded.
        if not isinstance(node, ast.stmt) or hasattr(node, "body") or not node.end_lineno:
            continue
        if node.end_lineno - node.lineno >= 40:
            continue
        inside = [ln for ln in touched if node.lineno <= ln <= node.end_lineno]
        if not inside:
            continue
        # Edits inside a lambda (class tables) are recorded on the lambda's own lines.
        lambdas = [(n.lineno, n.end_lineno) for n in ast.walk(node) if isinstance(n, ast.Lambda)]
        if lambdas and all(any(a <= ln <= b for a, b in lambdas) for ln in inside):
            continue
        out.add(node.lineno)
    return out


# ---------------------------------------------------------------- splitting


IMPROVE_MOVES = 64  # cap on move/swap steps per worker count


def _lb_ms(d: list[int], total: int, k: int) -> int:
    """Lower bound on the max load for k workers (d sorted descending)."""
    v = max(d[0], -(-total // k))
    if k < len(d):
        v = max(v, d[k - 1] + d[k])  # pigeonhole: k+1 items on k workers
    return v


def _lpt(order: list[int], ms: dict[int, int], k: int):
    loads = [0] * k
    bins: list[list[int]] = [[] for _ in range(k)]
    for n in order:
        i = loads.index(min(loads))
        bins[i].append(n)
        loads[i] += ms[n]
    return bins, loads


def _improve(bins, loads, ms, floor: int) -> None:
    """Move or swap an item off a max-load worker while the pair's max strictly drops."""
    k = len(bins)
    for _ in range(IMPROVE_MOVES):
        top = max(loads)
        if top <= floor:
            return
        srt = [sorted((ms[n], n) for n in b) for b in bins]
        keys = [[v for v, _ in s] for s in srt]
        best = None
        for mi in range(k):
            if loads[mi] != top:
                continue
            for bi in range(k):
                gap = top - loads[bi]
                if bi == mi or gap < 2:
                    continue
                kb = keys[bi]
                for x, xn in srt[mi]:
                    if x == 0:
                        continue
                    cands = [(x, None)]  # plain move
                    j = bisect.bisect_left(kb, x - gap // 2)  # swap partner near x - gap/2
                    for t in (j - 1, j):
                        if 0 <= t < len(kb):
                            cands.append((x - srt[bi][t][0], srt[bi][t][1]))
                    for d, yn in cands:
                        if 0 < d < gap:
                            key = (max(top - d, loads[bi] + d), mi, bi, xn, -1 if yn is None else yn)
                            if best is None or key < best[0]:
                                best = (key, d, mi, bi, xn, yn)
        if best is None:
            return
        _, d, mi, bi, xn, yn = best
        bins[mi].remove(xn)
        bins[bi].append(xn)
        if yn is not None:
            bins[bi].remove(yn)
            bins[mi].append(yn)
        loads[mi] -= d
        loads[bi] += d


def split(selected: set[int], ms: dict[int, int]) -> list[list[int]]:
    """The cheapest split: LPT plus improvement passes per worker count, stopping at the lower bound."""
    if not selected:
        return []
    order = sorted(selected, key=lambda n: (-ms[n], n))
    d = [ms[n] for n in order]
    total = sum(d)
    kmax = min(MAX_WORKERS, len(order))
    lbs = {k: _lb_ms(d, total, k) + WORKER_MS * k for k in range(1, kmax + 1)}
    best = None
    for k in sorted(lbs, key=lambda k: (lbs[k], k)):
        if best is not None and best[0] <= lbs[k]:
            break  # sorted by bound: no remaining k can win
        bins, loads = _lpt(order, ms, k)
        _improve(bins, loads, ms, lbs[k] - WORKER_MS * k)
        cost = max(loads) + WORKER_MS * k
        if best is None or (cost, k) < (best[0], best[2]):
            best = (cost, bins, k)
    return [sorted(b) for b in best[1] if b]


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    everything = set(ms)
    try:
        sel = Selector(tests)
    except Exception as e:  # map unreadable: run everything on every commit rather than print nothing
        log(f"selector init failed: {type(e).__name__}: {e}")
        sel = None
    for patch in sorted((DATA / "commits").glob("*.patch")):
        try:
            if sel is None:
                raise RuntimeError("no selector")
            # Bytes, not read_text: universal newlines would turn a lone "\r" into a line break.
            text = patch.read_bytes().decode("utf-8", "replace")
            files = parse_patch(text)
            if not files and text.strip():
                raise ValueError("no diff --git sections found")  # unreadable patch: run everything
            chosen, why = sel.select(files)
        except Exception as e:  # never crash: run everything for this commit
            chosen, why = set(everything), [f"error:{type(e).__name__}:{e}"]
        workers = split(chosen, ms)
        print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))
        log(patch.stem, len(chosen), " ".join(why))


if __name__ == "__main__":
    main()
