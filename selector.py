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
import subprocess
import sys
from pathlib import Path

DATA = Path(os.environ.get("DATA", "/data"))
APP = Path(os.environ.get("APP", "/app"))
SRC = Path(os.environ.get("SRC", APP / "src"))  # sqlglot checkout at the pinned commit
MAP = Path(os.environ.get("MAP", APP / "maps/impact.json.gz"))
INDEX = Path(os.environ.get("INDEX", APP / "maps/index.json"))  # built by tools/build_index.py
FAST = Path(os.environ.get("FAST", APP / "maps/fast.marshal"))  # selector.py --bake, at build time

# A dialect lives in one file per directory, all named after it.
DIALECT_DIRS = ("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/", "sqlglot/typing/", "sqlglot/tokenizers/")
TPCDS_TEST = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
TPC_PROBE = Path(os.environ.get("TPC_PROBE", APP / "tools/tpc_probe.py"))
TPC_BASE = Path(os.environ.get("TPC_BASE", APP / "maps/tpc_base.json"))  # tpc_probe.py on the pinned sources
TPC_QCOV = Path(os.environ.get("TPC_QCOV", APP / "maps/tpc_qcov.json"))  # tpc_probe.py --qcov: per-query lines
EXEC_BUDGET_S = 4.0  # tools/tpc_probe.py EXEC_BUDGET: query executions it may add
STMT_MAP = Path(os.environ.get("STMT_MAP", APP / "maps/stmt.map"))  # tools/build_stmt_map.py
STMT_PROBE = Path(os.environ.get("STMT_PROBE", APP / "tools/stmt_probe.py"))
PROBE_START_S = float(os.environ.get("PROBE_START_S", "0.15"))  # CPU per forked probe (fork, hot patch)
PROBE_SAFETY = float(os.environ.get("PROBE_SAFETY", "4.0"))  # estimated case time -> charged CPU
PROBE_PER_TEST_S = 0.04  # reduced-class build, setUpClass
PROBE_RATIO = float(os.environ.get("PROBE_RATIO", "1.5"))  # gain needed per CPU second (half the probed commits break something)
NO_STMT_PROBE = ("tests/test_executor.py",)  # heavy setUpClass (table loads)
PICKLE_FNS = ("__reduce__", "__reduce_ex__", "__getstate__", "__setstate__")
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


# Files whose constructions get validated against arg_types (see value_edit, P2).
VALIDATING = ("sqlglot/parser.py", "sqlglot/parsers/", "sqlglot/dialects/", "sqlglot/expressions/builders.py")


def load_maps() -> tuple[dict, dict]:
    """(coverage map, static index). FAST is a marshal snapshot of both, written at build time by
    `selector.py --bake` with the same interpreter (marshal is version-specific); it loads in
    about half the time of gzip+JSON. Any problem with it falls back to the JSON files."""
    try:
        import marshal
        m, index = marshal.loads(FAST.read_bytes())
        if isinstance(m, dict) and "sets" in m and isinstance(index, dict):
            return m, index
    except Exception as e:  # noqa: BLE001
        log(f"no fast map ({type(e).__name__}); reading JSON")
    with gzip.open(MAP, "rt") as fh:
        m = json.load(fh)
    try:
        index = json.loads(INDEX.read_text())
    except Exception as e:  # noqa: BLE001
        log(f"no index ({type(e).__name__}); import-time rules use file coverage only")
        index = {}
    return m, index


def bake() -> None:
    """Build step: write FAST from the JSON map and index (run after build_index.py)."""
    import marshal
    with gzip.open(MAP, "rt") as fh:
        m = json.load(fh)
    try:
        index = json.loads(INDEX.read_text())
    except Exception:  # noqa: BLE001 - same fallback the runtime would take
        index = {}
    FAST.write_bytes(marshal.dumps((m, index)))


class Selector:
    def __init__(self, tests: list[dict]):
        self.tests = tests
        self.by_id = {t["id"]: t["number"] for t in tests}
        self.all = {t["number"] for t in tests}
        self.slow = frozenset(t["number"] for t in tests if round(t["seconds"] * 1000) >= SLOW_MS)
        self.by_file: dict[str, set[int]] = {}
        for t in tests:
            self.by_file.setdefault(t["file"], set()).add(t["number"])
        m, index = load_maps()
        # Map test indexes in the map onto this run's numbers, by id.
        number_of = [self.by_id.get(tid) for tid in m["tests"]]
        self._number_of = number_of
        self._raw_sets = m["sets"]
        self._sets: dict[int, frozenset] = {}  # built on first use: a run touches few of them
        self.files = m["files"]
        self.strings = {number_of[int(k)]: v for k, v in m["test_strings"].items() if number_of[int(k)] is not None}
        self._src_cache: dict[str, str | None] = {}
        self.index = index

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

    def _string_index(self, attr: str, key) -> dict[str, set[int]]:
        """key(string) -> tests holding such a string; built once, on first use."""
        idx = self.__dict__.get(attr)
        if idx is None:
            idx = {}
            for n, strings in self.strings.items():
                for s in strings:
                    idx.setdefault(key(s), set()).add(n)
            self.__dict__[attr] = idx
        return idx

    def _head_index(self) -> dict[str, set[int]]:
        idx = self.__dict__.get("_by_head")
        if idx is None:
            idx = {}
            for n, strings in self.strings.items():
                for s in strings:
                    if "," in s and "=" in s:
                        idx.setdefault(s.split(",", 1)[0].strip(), set()).add(n)
            self._by_head = idx
        return idx

    def name_tests(self, words: set[str]) -> set[int]:
        """Tests in test files named after any word, e.g. snowflake -> tests/dialects/test_snowflake.py,
        plus tests holding the word as a whole string, e.g. dialect="snowflake" or write={"snowflake": ...}."""
        out: set[int] = set()
        for w in words:
            for f, nums in self.by_file.items():
                if Path(f).stem == f"test_{w}":
                    out |= nums
        if words:
            by_string = self._string_index("_by_string", lambda s: s)
            by_head = self._head_index()
            for w in words:
                out |= by_string.get(w, set())
                # Dialect strings with settings: "duckdb, version=1.0".
                out |= by_head.get(w, set())
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

    def class_tests(self, name: str, where: tuple[str, ...] | None = None) -> set[int] | None:
        """Coverage of every line naming the class or a subclass, and of their `<lower>_sql` methods.

        With `where`, only references in files starting with those prefixes count, and None is
        returned when one of them sits outside any function (a table built at import: coverage
        credits it to whichever test came first, so it says nothing about its users)."""
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
                if where is not None:
                    if not rel.startswith(where):
                        continue
                    if not self.innermost(rel, start)[0] or self.in_import_fn(rel, start):
                        return None
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

    def fixture_readers(self, fixture_rel: str) -> tuple[set[int], str]:
        """Tests whose strings name the fixture file, and how they were found.

        A string names it when its last path component is the file's name or stem ("simplify",
        "optimizer/qualify_columns__invalid.sql", "tpc-h/tpc-h"). Directory strings that every test
        of a class carries ("optimizer/" from a helper, "/optimizer/tpc-ds/" constants) don't, which
        substring matching got wrong ("optimizer" matched all 60 TestExecutor tests). Substring
        and directory matches remain as fallbacks when nothing names the file.
        """
        memo = self.__dict__.setdefault("_fx_memo", {})
        if fixture_rel not in memo:
            path = Path(fixture_rel)
            name, stem, parent = path.name, path.stem, path.parent.name
            by_tail = self._string_index("_by_tail", lambda s: s.rsplit("/", 1)[-1])
            hits = by_tail.get(name, set()) | by_tail.get(stem, set())
            how = "fixture-name"
            if not hits:
                hits = {n for n, strings in self.strings.items() if any(stem in s for s in strings)}
                how = "fixture-stem"
            if not hits and parent:
                hits = {n for n, strings in self.strings.items() if any(parent in s for s in strings)}
                how = "fixture-dir"
            memo[fixture_rel] = (frozenset(hits), how)
        hits, how = memo[fixture_rel]
        return set(hits), how

    def setup_fixture_readers(self, f: dict) -> set[int] | None:
        """A fixture a test class loads in setUpClass into a class attribute (index setup_fixtures):
        only the tests using that attribute read it. The loaders split text on ";" and parse "#"
        meta lines; a "#" line without ":" raises inside setUpClass and errors the whole class, so
        an edit touching any line starting with "#" is not narrowed. Tests naming the file in their
        own code still count."""
        table = self.index.get("setup_fixtures")
        if not table:
            return None
        for h in f["hunks"]:
            for tag, text, _ in h["lines"]:
                if tag in ("+", "-") and text.lstrip().startswith("#"):
                    return None
        path = f["path"]
        keys = [k for k in table if path.endswith(k.lstrip("/"))]
        if not keys:
            return None
        out = {self.by_id[t] for k in keys for t in table[k] if t in self.by_id}
        if not out:
            return None
        # Other tests that name the file outside that class helper (their own strings) still read it.
        name = Path(path).name
        class_of = {t.rsplit("::", 1)[0] for k in keys for t in table[k]}
        by_tail = self._string_index("_by_tail", lambda s: s.rsplit("/", 1)[-1])
        for n in by_tail.get(name, set()) | by_tail.get(Path(path).stem, set()):
            tid = self.tests[n]["id"] if n < len(self.tests) else ""
            if tid.rsplit("::", 1)[0] not in class_of:
                out.add(n)
        return out

    def fixture_tests(self, fixture_rel: str) -> set[int]:
        return self.fixture_readers(fixture_rel)[0]

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

    def import_time_change(self, rel: str, line: int, extra_names: set[str] = frozenset(),
                           import_safe: bool = False) -> set[int]:
        # Same answer for every line of one top-level class (or module level): memoise.
        key = (rel, self.top_class(rel, line), frozenset(extra_names), import_safe)
        memo = self.__dict__.setdefault("_itc_memo", {})
        if key not in memo:
            memo[key] = self._import_time_change(rel, line, extra_names, import_safe)
        return set(memo[key])

    def _import_time_change(self, rel: str, line: int, extra_names: set[str] = frozenset(),
                            import_safe: bool = False) -> set[int]:
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
            # Construct-only ones (test_generator_memory: builds every dialect's generator and
            # measures memory, never reads the tables) can fail only if the import or the generator
            # construction changes, so they are skipped when the edit is import-safe (see
            # import_safe); the others assert on dialect values and always stay.
            skip = set(self.index.get("construct_only_tests", ())) if import_safe else set()
            for tid in self.index.get("all_dialect_tests", []):
                n = self.by_id.get(tid)
                if n is not None and tid not in skip:
                    hit.add(n)
        if rel.startswith("sqlglot/executor/"):
            # Module-level executor state (the ENV builtins table) is read by every executor test,
            # including ones whose coverage never touches the edited module.
            for f in ("sqlglot/executor/python.py", "sqlglot/generators/python.py"):
                hit |= self.file_cov(f)
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

    # -- proofs that an edit cannot matter

    def equivalent_swap(self, old_tree, new_tree, rel: str) -> str | None:
        """The only difference is one `is` <-> `==` (or `is not` <-> `!=`) swap that provably gives the
        same result: a reason string, else None.

        Against None: the left side's annotated type (and every ancestor and subclass of it) has
        no __eq__/__ne__ that reads `other` without a type guard, so `x == None` is `x is None`.
        Against a member of a pure Enum: the same type test. Against a member of a str/int-mixin
        enum (where "UPPERCASE" == member): every value ever stored under that attribute name is a
        member of that enum or None (index attr_enum), so equality is identity.
        """
        ix = self.index
        if "eq_unsafe" not in ix:
            return None
        diffs = leaf_diffs(old_tree, new_tree)
        if not diffs or len(diffs) != 1 or diffs[0][0] != "cmp":
            return None
        _, cmp_, new_cmp, i = diffs[0]
        pair = (type(cmp_.ops[i]), type(new_cmp.ops[i]))
        if pair not in {(ast.Is, ast.Eq), (ast.Eq, ast.Is), (ast.IsNot, ast.NotEq), (ast.NotEq, ast.IsNot)}:
            return None
        left, right = ([cmp_.left] + cmp_.comparators)[i], cmp_.comparators[i]
        if isinstance(left, ast.Constant) and left.value is None:
            left, right = right, left
        if isinstance(right, ast.Constant) and right.value is None:
            return "is-none-swap" if self.eq_safe_type(old_tree, left, rel) else None
        if isinstance(right, ast.Attribute) and isinstance(right.value, (ast.Name, ast.Attribute)):
            enum = ast.unparse(right.value).split(".")[-1]
            kind = ix.get("enum_kind", {}).get(enum)
            if kind == "pure" and self.eq_safe_type(old_tree, left, rel):
                return "is-enum-swap"
            if kind == "mixin" and isinstance(left, ast.Attribute) and ix.get("attr_enum", {}).get(left.attr) == enum:
                return "is-enum-member-swap"
        return None

    def eq_safe_type(self, tree, expr, rel: str) -> bool:
        """expr's annotation names only classes whose equality against None or a pure enum member
        is identity: builtins, or repo classes with no unguarded __eq__ in their whole hierarchy."""
        ix = self.index
        ann = None
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name) and expr.value.id == "self":
            ann = ix.get("attr_ann", {}).get(f"{rel}:{expr.attr}")
        elif isinstance(expr, ast.Name):
            for fn in walk(tree):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.lineno <= expr.lineno <= fn.end_lineno:
                    for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs:
                        if a.arg == expr.id:
                            ann = ast.unparse(a.annotation) if a.annotation is not None else None
                            # A rebinding anywhere in the function voids the annotation as a proof.
                            if any(isinstance(n, ast.Name) and n.id == expr.id and isinstance(n.ctx, ast.Store)
                                   for n in walk(fn)):
                                return False
        if not ann:
            return False
        names = set(re.findall(r"[A-Za-z_]\w*", ann)) - {
            "t", "typing", "exp", "None", "Optional", "Union", "int", "str", "bool", "float", "bytes",
            "list", "dict", "tuple", "set", "frozenset", "List", "Dict", "Tuple", "Set", "Sequence"}
        bases, subs, unsafe = ix.get("class_bases", {}), ix.get("class_subs", {}), ix.get("eq_unsafe", {})
        todo, seen = list(names), set()
        while todo:
            n = todo.pop()
            if n in seen:
                continue
            seen.add(n)
            if n in SAFE_BUILTIN_BASES:
                continue  # their __eq__ returns NotImplemented for a foreign right side
            if n not in bases:
                return False  # Any, a TypeVar, a third-party or unknown class: no proof
            if unsafe.get(n):
                return False
            todo.extend(bases.get(n, ()))
            todo.extend(subs.get(n, ()))
            if len(seen) > 2000:
                return False
        return True

    def import_safe(self, old_tree, new_tree, rel: str) -> bool:
        """The edit only swaps literal values in a way that cannot change what importing the module or
        building a generator does: each changed constant keeps its type, is not a dict key, and sits
        in displays (dict/list/tuple/set, unary +/-) of a class-body or module-level assignment whose
        target building a generator never reads; it keeps its truthiness unless it is only captured
        by a closure factory (_show_parser("X", target=True)) or a lambda body."""
        ix = self.index
        if rel == "sqlglot/dialects/dialect.py" or "gen_init_attrs" not in ix or "closure_factories" not in ix:
            return False
        diffs = leaf_diffs(old_tree, new_tree)
        if not diffs or any(d[0] != "const" for d in diffs):
            return False
        parents = {}
        for p in walk(old_tree):
            for c in ast.iter_child_nodes(p):
                parents[id(c)] = p
        factories, gen_attrs = set(ix["closure_factories"]), set(ix["gen_init_attrs"])
        for _, a, b in diffs:
            if type(a.value) is not type(b.value) or type(a.value) not in (int, float, str, bool, bytes):
                return False
            node, deferred = a, False
            while True:
                p = parents.get(id(node))
                if p is None:
                    return False
                if isinstance(p, ast.Dict):
                    if any(k is node for k in p.keys):
                        return False  # a key changed: lookups and registrations change
                elif isinstance(p, ast.Lambda):
                    if node is not p.body:
                        return False  # a default argument
                    deferred = True
                elif isinstance(p, ast.Call):
                    if node is p.func or call_name(p) not in factories:
                        return False
                    deferred = True
                elif isinstance(p, ast.UnaryOp):
                    if not isinstance(p.op, (ast.USub, ast.UAdd)):
                        return False
                elif isinstance(p, ast.stmt):
                    holder = parents.get(id(p))
                    if not isinstance(p, (ast.Assign, ast.AnnAssign)) or not isinstance(holder, (ast.ClassDef, ast.Module)):
                        return False
                    targets = p.targets if isinstance(p, ast.Assign) else [p.target]
                    if any(not isinstance(tg, ast.Name) or tg.id in gen_attrs for tg in targets):
                        return False
                    if not deferred and bool(a.value) != bool(b.value):
                        return False
                    break
                elif not isinstance(p, (ast.List, ast.Tuple, ast.Set, ast.keyword)):
                    return False
                node = p
        return True

    def dialect_users(self, rel: str) -> set[int]:
        """Tests that can use the dialect `rel` belongs to (or a subclass of it): they run its code,
        name it (strings, fixture headers, test file), name its classes in code, or loop over all
        dialects."""
        family = self.dialect_family(rel)
        memo = self.__dict__.setdefault("_users_memo", {})
        key = frozenset(family)
        if key not in memo:
            out: set[int] = set()
            for f2 in family:
                out |= self.file_cov(f2)
            out |= self.name_tests({Path(f2).stem for f2 in family} - {"__init__", "base"})
            idents = self.index.get("test_idents", {})
            for f2 in family:
                for start, end, name in self.index.get("toplevel", {}).get(f2, []):
                    out |= {self.by_id[t] for t in idents.get(name, ()) if t in self.by_id}
            out |= {self.by_id[t] for t in self.index.get("all_dialect_tests", ()) if t in self.by_id}
            memo[key] = frozenset(out)
        return set(memo[key])

    def value_edit(self, old_tree, new_tree, rel: str, sink: dict | None = None) -> tuple[set[int], str] | None:
        """The edit only changes literal values held by class attributes: the tests that read them.

        P1, a class constant (UPPER_CASE): coverage of every runtime line reading that attribute
        name (index attr_reads), plus tests reading it in their own code. Gives up (None: use the
        import-time rule) when a reader runs at import (class body, module level, metaclass or
        other import-time function) unless it only looks at the keys of a dict whose values
        changed, or when the name is read through a string (getattr). Reads from an unrelated
        class (MySQLParser.SHOW_PARSERS while editing SingleStoreParser's) don't count. In a
        dialect file the result is limited to tests that use that dialect family.
        P2, an Expression's arg_types values (required flags): the tests that build or parse
        that class (its references and SQL names), not the whole file's coverage.
        """
        ix = self.index
        if "attr_reads" not in ix or "closure_factories" not in ix:
            return None
        diffs = leaf_diffs(old_tree, new_tree)
        if not diffs or any(d[0] != "const" for d in diffs):
            return None
        parents = {}
        for p in walk(old_tree):
            for c in ast.iter_child_nodes(p):
                parents[id(c)] = p
        factories = set(ix["closure_factories"])
        edits: set[tuple[str, str, bool]] = set()  # (class, attribute, only dict values changed)
        for _, a, b in diffs:
            if type(a.value) is not type(b.value):
                return None
            node, in_value = a, False
            while True:
                p = parents.get(id(node))
                if p is None:
                    return None
                if isinstance(p, ast.Dict):
                    if any(k is node for k in p.keys):
                        return None
                    in_value = True
                elif isinstance(p, ast.Call):
                    if node is p.func or call_name(p) not in factories:
                        return None
                elif isinstance(p, ast.UnaryOp):
                    if not isinstance(p.op, (ast.USub, ast.UAdd)):
                        return None
                elif isinstance(p, (ast.Assign, ast.AnnAssign)):
                    holder = parents.get(id(p))
                    targets = p.targets if isinstance(p, ast.Assign) else [p.target]
                    if not isinstance(holder, ast.ClassDef) or len(targets) != 1 or not isinstance(targets[0], ast.Name):
                        return None
                    edits.add((holder.name, targets[0].id, in_value))
                    break
                elif not isinstance(p, (ast.List, ast.Tuple, ast.Set, ast.keyword)):
                    return None
                node = p
        if not edits:
            return None
        if all(attr == "arg_types" and in_value for _, attr, in_value in edits) and rel.startswith("sqlglot/expressions/"):
            out: set[int] = set()
            for cls, _, _ in edits:
                # Required flags are read only through required_args -> error_messages(), which
                # only Parser.validate_expression and exp.func() call: the instances that matter
                # are the ones parser code (and the builders) construct.
                built = self.class_tests(cls, VALIDATING)
                out |= built if built is not None else self.class_tests(cls)
                by_name = self.sql_name_tests(cls, set())
                if by_name is None:
                    return None
                out |= by_name
            return (out, "arg-types") if out else None
        if not all(CONST_NAME.match(attr) for _, attr, _ in edits):
            return None
        subs_all = ix.get("class_subs", {})
        known = ix.get("class_bases", {})
        out = set()
        for cls, attr, in_value in edits:
            family, todo = set(), [cls]
            while todo:
                n = todo.pop()
                if n not in family:
                    family.add(n)
                    todo.extend(subs_all.get(n, ()))
            reads = ix["attr_reads"].get(attr)
            if not reads:
                return None
            lines: dict[str, set[int]] = {}
            expanded = []
            for r in reads:
                if len(r) > 5 and not r[4]:
                    # Passed on as a keyword argument (TokenizerCore(numeric_escapes=self.NUMERIC_ESCAPES)):
                    # the call runs on every use; the readers are the callee's parameter and `.K` loads.
                    downstream = ix.get("hop_reads", {}).get(r[5])
                    if not downstream or r[2] != "fn" or self.in_import_fn(r[0], r[1]):
                        return None  # untraceable, or the call itself runs at import
                    expanded += [[f2, line, where, "", 0] for f2, line, where in downstream]
                else:
                    expanded.append(r[:5])
            for f2, line, where, owner, keyonly in expanded:
                if owner.startswith("class:"):
                    if owner[6:] not in family:
                        continue  # a bare name in another class's body is that class's own attribute
                elif owner in known and owner not in family:
                    continue  # read from an unrelated class
                if where == "dyn":
                    return None
                if keyonly and in_value:
                    continue  # only the dict's keys are looked at, and only values changed
                if where != "fn" or self.in_import_fn(f2, line):
                    return None  # read at import: its effect is credited to whichever test came first
                lines.setdefault(f2, set()).add(line)
            hit: set[int] = set()
            for f2, lns in lines.items():
                hit |= self.cov(f2, lns)
                if sink is not None:
                    sink.setdefault("lines", {}).setdefault(f2, set()).update(lns)
            if sink is not None:
                sink.setdefault("keep", set()).update(
                    self.by_id[t] for t in ix.get("test_attr_users", {}).get(attr, ()) if t in self.by_id)
            if rel.startswith(DIALECT_DIRS):
                hit &= self.dialect_users(rel)
            hit |= {self.by_id[t] for t in ix.get("test_attr_users", {}).get(attr, ()) if t in self.by_id}
            out |= hit
        return out, "value-readers"

    def ran_at_import(self, rel: str, old_tree, ln: int) -> bool:
        """Did the statement holding `line` (inside a function body) run outside any test, at
        import or collection time? (map "imp": lines whose coverage has the empty context)"""
        imp = self.files.get(rel, {}).get("imp")
        if not imp:
            return False
        memo = self.__dict__.setdefault("_imp_memo", {})
        if rel not in memo:
            memo[rel] = frozenset(imp)
        span = statement_span(old_tree, ln) or (ln, ln)
        return any(x in memo[rel] for x in range(span[0], span[1] + 1))

    def default_edit(self, old_tree, new_tree, rel: str) -> set[int] | None:
        """The edit only flips constant defaults of parameters: the tests covering call sites that
        rely on them (no keyword for it, too few positionals, or *args/**kwargs that might not pass
        it), and those passing the function on as a callback (it's then called with what the
        receiver chooses). None (fall back to the whole function) when the function is dispatched
        by name (`*_sql`, getattr strings), referenced at import time (tables, RULES), or called
        from import-time code. In a dialect file, limited to that dialect's users."""
        ix = self.index
        if "call_sites" not in ix:
            return None
        diffs = leaf_diffs(old_tree, new_tree)
        if not diffs or any(d[0] != "const" for d in diffs):
            return None
        params: list[tuple[str, str, int | None]] = []  # (function, parameter, positional index)
        for d in diffs:
            found = None
            for fn in walk(old_tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                a = fn.args
                pos = a.posonlyargs + a.args
                for i, dv in enumerate(a.defaults):
                    if dv is d[1]:
                        k = len(pos) - len(a.defaults) + i
                        bound = 1 if pos and pos[0].arg in ("self", "cls") else 0
                        found = (fn.name, pos[k].arg, k - bound)
                for arg, dv in zip(a.kwonlyargs, a.kw_defaults):
                    if dv is d[1]:
                        found = (fn.name, arg.arg, None)
            if found is None:
                return None
            params.append(found)
        dyn = set(ix.get("dyn_names", ()))
        out: set[int] = set()
        for name, param, k in params:
            if name.endswith("_sql") or name in dyn or name.startswith("__"):
                return None

            def relies(npos: int, kws: str, star: int) -> bool:
                if star or (k is None and False):
                    return True
                if param in kws.split(","):
                    return False
                return k is None or npos <= k

            for f2, s0, e0, npos, kws, star, in_fn in ix["call_sites"].get(name, ()):
                if not relies(npos, kws, star):
                    continue
                if not in_fn or self.in_import_fn(f2, s0):
                    return None  # called at import: coverage credits whichever test came first
                hit = self.cov(f2, range(s0, e0 + 1))
                if not hit and f2 not in self.files:
                    return None  # unmeasured caller
                out |= hit
            for f2, s0, e0, where in ix.get("value_refs", {}).get(name, ()):
                if where != "fn" or self.in_import_fn(f2, s0):
                    return None
                out |= self.cov(f2, range(s0, e0 + 1))
            keys = [c[0] for c in ix.get("call_site_tests", {}).get(name, ()) if relies(c[1], c[2], c[3])]
            keys += ix.get("value_ref_tests", {}).get(name, [])
            for key in keys:
                if key.endswith("::"):
                    out |= {n for t, n in self.by_id.items() if t.startswith(key)}
                elif key in self.by_id:
                    out.add(self.by_id[key])
        if rel.startswith(DIALECT_DIRS) and Path(rel).stem not in ("__init__", "base"):
            out &= self.dialect_users(rel)
        return out

    def dead_edit(self, f: dict, old_tree, new_tree, rel: str, ln: int, fn: tuple[int, int]) -> bool:
        """The changed statement never ran in any test or at import, inside a function that did run,
        and the edit changes no name's scope: no test can observe it (P3). Only for files whose
        coverage is direct (dialect-specific modules, typing/ excluded: test_typing reads source),
        and only for one-for-one same-indent line replacements."""
        if not rel.startswith(DIALECT_DIRS) or rel.startswith("sqlglot/typing/") or Path(rel).stem in ("__init__", "base"):
            return False
        table = self.files.get(rel, {}).get("lines", {})
        if not table or not any(str(x) in table for x in range(fn[0], fn[1] + 1)):
            return False  # unmeasured file, or a function that never ran: no proof
        stmt = None
        for node in walk(old_tree):
            if isinstance(node, ast.stmt) and node.lineno <= ln <= (node.end_lineno or node.lineno):
                body = getattr(node, "body", None)
                head_end = body[0].lineno - 1 if body and getattr(body[0], "lineno", None) else node.end_lineno
                if node.lineno <= ln <= head_end and (stmt is None or node.lineno >= stmt.lineno):
                    stmt = node
        if stmt is None or isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return False
        if str(stmt.lineno) in table:
            return False  # it ran (or is recorded): not dead
        memo = self.__dict__.setdefault("_scope_memo", {})
        key = id(f)
        if key not in memo:
            try:
                old_src = self.source(f["old"]) or ""
                new_src = apply_patch(old_src, f) or ""
                memo[key] = scope_signature(old_src, rel) == scope_signature(new_src, rel)
            except Exception:
                memo[key] = False
        return memo[key]

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
            self._patched[rel] = (old_tree, new_tree, new)
            same = same_tree(old_tree, new_tree)
            how = "ast-identical"
            if not same and not rel.startswith("sqlglot/typing/"):
                # Docstring-only edits: pytest collects no doctests here and nothing reads
                # docstrings (typing/ excluded: test_typing compares code constants).
                strip_docstrings(old_tree)
                strip_docstrings(new_tree)
                same = same_tree(old_tree, new_tree)
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
            if rel.startswith("sqlglot/") and not rel.startswith("sqlglot/typing/") and bytes_inert(f):
                why = self.equivalent_swap(old_tree, new_tree, rel)
                if why:
                    return set(), why
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
        if not any(self.innermost(rel, ln)[0] for ln in changed):
            # Only class bodies / module level changed: literal value edits go to their readers.
            ve = self.value_edit(old_tree, new_tree, rel)
            if ve is not None:
                hit, why = ve
                if rel.startswith("sqlglot/typing/"):
                    hit |= self.by_file.get(TYPING_TEST, set())
                return hit, "sqlglot:" + why
        headers = def_headers(old_tree)
        safe = None  # import_safe(), computed on first module-level line
        lam_only = None  # lambda_body_only(), likewise
        leaf_ok = None  # leaf_diffs() is not None, likewise
        default_hit = None  # default_edit(), likewise (False: not applicable)
        out: set[int] = set()
        reasons = []
        for ln in sorted(changed):
            fn, cls = self.innermost(rel, ln)
            if fn and ln in headers and default_hit is None:
                default_hit = self.default_edit(old_tree, new_tree, rel) or False
            if fn and ln in headers and default_hit:
                # An affected test calls the function, so it also runs its body: never wider than
                # the whole-function rule below.
                s0, e0 = headers[ln]
                body = self.cov(rel, range(s0, e0 + 1))
                out |= (default_hit & body) if body else default_hit
                reasons.append("default-callers")
            elif fn and ln in headers:
                # Defaults and decorators run when the def runs, so coverage credits them to whichever
                # test imported the module first (lazily loaded optimizer/executor/dialect modules).
                # They matter to every call: take the whole function's tests, nested closures
                # included (a factory's default reaches its closure's callers). Never pruned.
                s, e = headers[ln]
                hit = self.cov(rel, range(s, e + 1))
                if not hit:
                    hit = self.import_time_change(rel, ln) or set(self.all)
                out |= hit
                reasons.append("def-header")
            elif fn and self.in_import_fn(rel, ln):
                # Runs at import or class creation (hooks, metaclasses, helpers they call):
                # coverage only credits whichever test imported first.
                hit = self.import_time_change(rel, ln) | self.cov(rel, range(fn[0], fn[1] + 1))
                if not hit:
                    return set(self.all), "import-fn-unmapped"
                out |= hit
                reasons.append("import-fn")
            elif fn and self.ran_at_import(rel, old_tree, ln):
                # This function body ran while modules were being imported (class bodies parse SQL
                # templates: DuckDBGenerator's *_TEMPLATE = exp.maybe_parse(...) runs the tokenizer,
                # the parser and its recoverable-error path). Coverage credits that run to whichever
                # test imported first; an exception here fails collection of every test module that
                # imports the dialect, and a changed value is read anywhere. Run everything.
                return set(self.all), "sqlglot:ran-at-import"
            elif fn and ln in prunable and self.dead_edit(f, old_tree, new_tree, rel, ln, fn):
                reasons.append("dead")
            elif fn:
                lo, hi = max(fn[0], ln - WINDOW), min(fn[1], ln + WINDOW)
                hit = self.cov(rel, range(lo, hi + 1))
                if leaf_ok is None:
                    leaf_ok = leaf_diffs(old_tree, new_tree) is not None
                span = statement_span(old_tree, ln) if leaf_ok and ln in removed else None
                if span and ln > self.body_start(rel, fn) and not self.lambda_range(rel, ln):
                    # A leaf edit (same tree shape: a constant, operator or comparison changed in
                    # place) can only be seen by a test that executed its statement. Coverage
                    # records a multi-line statement on any of its lines, so take them all (the
                    # head only, for an if/while/for whose condition changed); the window's
                    # neighbouring lines add nothing. Lambdas run later from elsewhere: excluded.
                    exact = self.cov(rel, range(span[0], span[1] + 1))
                    if exact:
                        hit = exact
                        reasons.append("stmt")
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
                    elif not table:
                        # The file was never measured (no line of it recorded): no coverage proof
                        # that nothing runs it. Treat it like a module-level edit, else run everything.
                        hit = self.import_time_change(rel, ln) or set(self.all)
                        reasons.append("fn-unmeasured")
                out |= hit
                reasons.append("fn")
            else:
                lam = self.lambda_range(rel, ln)
                hit = self.cov(rel, range(lam[0], lam[1] + 1)) if lam else set()
                if lam_only is None:
                    lam_only = lambda_body_only(old_tree, new_tree)
                if hit and ln in removed and (ln != lam[0] or lam_only):
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
                if safe is None:
                    safe = self.import_safe(old_tree, new_tree, rel)
                hit = self.import_time_change(rel, ln, renamed, safe)
                if not hit:
                    return set(self.all), "module-level-unmapped"
                out |= hit
                reasons.append("module-safe" if safe else "module")
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
        # A removed class attribute or method that another module reads as Cls.attr (a deleted
        # TokenType member, a dropped flag) breaks that module's import, and with it `import sqlglot`.
        qualified = self.index.get("qualified_attrs", {})
        for owner, n in class_bound(old_tree) - class_bound(new_tree):
            if any(f2 != rel for f2 in qualified.get(f"{owner}.{n}", ())):
                return set(self.all), "sqlglot:lost-class-attr"
        new_names = def_names(new_tree, nested=False) - def_names(old_tree, nested=False)
        if new_names:
            # A new or renamed method can override a parent's or be found by name
            # (generators look up `<lower expression>_sql`): take every caller of that name.
            # Nested helpers are local to their function (its own lines already count). In a
            # dialect file only that dialect's users can reach the override.
            hit = self.new_method_tests(new_names)
            if rel.startswith(DIALECT_DIRS) and Path(rel).stem not in ("__init__", "base"):
                hit &= self.dialect_users(rel)
            out |= hit
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
        hits, how = self.fixture_readers(f["path"])
        narrowed = self.setup_fixture_readers(f) if f["old"] == f["path"] else None
        if narrowed is not None:
            return narrowed, "fixture-setup-attr"
        if f["old"] and f["old"] != f["path"]:
            more, _ = self.fixture_readers(f["old"])  # readers of the old name break too
            hits |= more
        if hits:
            return hits, how
        return set(self.all), "fixture-unmapped"

    def select(self, files: list[dict]) -> tuple[set[int], list[str]]:
        out: set[int] = set()
        why = []
        self._patched: dict[str, tuple] = {}  # rel -> (old tree, new tree, new source), by python_change
        clear_tree_caches()  # trees are per commit; keep memory flat
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
        # The TPC probe first: its verdict depends only on the patch, and the statement probe
        # weighs its gains against the cost of what remains (with test_execute_tpcds, 21.5 s,
        # still selected, dropping the small tests beside it looks worthless).
        tpcds = self.by_id.get(TPCDS_TEST)
        if tpcds in out and self.tpc_probe_clears(files):
            out.discard(tpcds)
            why.append("tpc-probe-cleared")
        if out and os.environ.get("STMT_OFF") != "1":
            try:
                gone, how = self.stmt_probe(out, files)
            except Exception as e:  # noqa: BLE001 - no proof: keep everything
                gone, how = set(), f"stmt-error:{type(e).__name__}:{e}"
            if gone or how:
                out -= gone
                why.append(f"stmt[{how}]-{len(gone)}")
        return out, why

    def tpc_probe_clears(self, files: list[dict]) -> bool:
        """test_execute_tpcds (21 s) can't see this commit: run tools/tpc_probe.py on the patched
        sources and compare with the build-time fingerprints.

        The test parses each TPC-DS query (parent), pickles it to a child that runs execute():
        optimize(), then the planner and the Python executor on the data, and compares with DuckDB
        results for transpile(sql, write="duckdb"). The probe reproduces parse_one, optimize and
        transpile exactly. The rest runs code the edit must not touch: every changed line is
        inside a function body (not a def header, not run at import), in a statement the test
        never runs outside those three calls (map "tpc_out", from tools/coverage_tpc.sh), and the
        edit only changes leaves in place (no new or removed definitions that could change what
        the rest of the test calls). Then unchanged code gets identical input (serde.dump
        fingerprints: everything pickling sends) and behaves identically. A probe that is much
        slower than the baseline (a timeout risk), errors, or differs anywhere keeps the test."""
        if not TPC_BASE.exists() or not TPC_PROBE.exists():
            return False
        overlay: dict[str, str] = {}
        reach: dict[str, set[int]] = {}  # old lines whose behaviour the edit changes
        for f in files:
            rel = f["path"] or ""
            if f["binary"] or f["rename"] or f["created"] or f["deleted"] or f["old"] != rel:
                return False
            if rel.startswith("tests/"):
                if rel.endswith(".py") or "/tpc-ds/" in rel:
                    return False  # the test, its helpers, or its data
                continue  # another fixture: read by other tests only
            if not rel.startswith("sqlglot/") or not rel.endswith(".py"):
                return False
            rec = self._patched.get(rel)
            if rec is None:
                return False
            old_tree, new_tree, new_src = rec
            if same_tree(old_tree, new_tree):
                continue
            if rel == "sqlglot/serde.py" or leaf_diffs(old_tree, new_tree) is None:
                return False
            outside = self.files.get(rel, {}).get("tpc_out")
            if outside is None:
                return False
            outside = set(outside)
            headers = def_headers(old_tree)
            pickling = [(n.lineno, n.end_lineno) for n in walk(old_tree)
                        if isinstance(n, ast.FunctionDef) and n.name in PICKLE_FNS]
            defs = {n.lineno: n for n in walk(old_tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            for ln in touched_old_lines(f):
                fn, _ = self.innermost(rel, ln)
                if fn and ln in headers:
                    # A signature line (a constant default, by leaf_diffs): it runs at import and
                    # can't raise there; it only changes calls of this function. Fine if the rest
                    # of the test never runs the body. Decorator lines (they run code) are not.
                    s0, e0 = headers[ln]
                    node = next((d for d in defs.values() if d.lineno <= ln and (d.end_lineno or 0) >= e0
                                 and min([d.lineno] + [x.lineno for x in d.decorator_list]) == s0), None)
                    if node is None or ln < node.lineno or any(x in outside for x in range(s0, e0 + 1)):
                        return False
                    reach.setdefault(rel, set()).update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
                    continue
                if not fn or self.ran_at_import(rel, old_tree, ln):
                    return False
                if any(a <= ln <= b for a, b in pickling):
                    return False
                span = statement_span(old_tree, ln) or (ln, ln)
                if any(x in outside for x in range(span[0], span[1] + 1)):
                    return False
                reach.setdefault(rel, set()).update(range(span[0], span[1] + 1))
            overlay[rel] = new_src
        if not overlay:
            return False
        only = self.tpc_queries(reach)
        if probe_allowance() < 2 * json.loads(TPC_BASE.read_text()).get("t", 10) + 15:
            log("tpc probe skipped: batch time")
            return False
        try:
            base = json.loads(TPC_BASE.read_text())
            tmp = Path(os.environ.get("TMPDIR", "/tmp")) / f"tpc_overlay_{os.getpid()}.json"
            tmp.write_text(json.dumps(overlay))
            limit = 2 * base["t"] + 5 + 2 * EXEC_BUDGET_S
            _t0 = os.times()
            args = [sys.executable, str(TPC_PROBE), str(SRC), str(tmp), str(TPC_BASE)]
            if only:
                args.append(",".join(map(str, sorted(only))))
            log(f"tpc probe queries {len(only) if only else 'all'}")
            r = subprocess.run(args,
                               capture_output=True, text=True, timeout=limit + 30,
                               env=dict(os.environ, PYTHONHASHSEED="0"))
            _t1 = os.times()
            log(f"tpc probe cpu {(_t1.children_user + _t1.children_system - _t0.children_user - _t0.children_system):.2f}s")
            tmp.unlink(missing_ok=True)
            got = json.loads(r.stdout.strip().splitlines()[-1])
        except Exception as e:  # noqa: BLE001 - no proof: keep the test
            log(f"tpc probe failed: {type(e).__name__}: {e}")
            return False
        if not base["h"] or len(got["h"]) != len(base["h"]) or got["t"] > limit:
            return False
        settled = got.get("x", {})
        for i, (a, b) in enumerate(zip(got["h"], base["h"])):
            # A query whose inputs differ passes only if executing it gave the pinned code's result.
            if a != b and (str(i) not in settled or settled[str(i)] != base.get("x", {}).get(str(i))):
                return False
        return True


    def tpc_queries(self, reach: dict[str, set[int]]) -> set[int] | None:
        """TPC-DS queries whose parse/optimize/transpile runs any of these lines (cold, per query:
        tools/tpc_probe.py --qcov). The others run unchanged code on unchanged input and keep their
        build-time fingerprints. None (recompute all) without the map or when nothing is reached."""
        try:
            q = self.__dict__.get("_tpc_qcov")
            if q is None:
                q = self._tpc_qcov = json.loads(TPC_QCOV.read_text())
            n = q["n"]
            if n != len(json.loads(TPC_BASE.read_text())["h"]):
                return None
            mask = 0
            for rel, lns in reach.items():
                d = q["files"].get(rel)
                if d is None:
                    continue
                for ln in lns:
                    mask |= d.get(str(ln), 0)
        except Exception as e:  # noqa: BLE001 - no map: recompute everything
            log(f"tpc qcov unusable: {type(e).__name__}: {e}")
            return None
        only = {i for i in range(n) if (mask >> i) & 1}
        return only or None

    # -- statement probe

    def local_lines(self, files: list[dict]) -> tuple[dict[str, set[int]], dict[str, str], set[int]] | str:
        """Old lines whose statements behave differently after the commit, when every change is a
        leaf edit in place (constant, operator, comparison) inside a function or lambda body that
        never ran at import. Then code outside those statements runs exactly as before, on the
        same inputs, as long as no edited statement ran. None when that can't be shown."""
        lines: dict[str, set[int]] = {}
        overlay: dict[str, str] = {}
        keep: set[int] = set()
        for f in files:
            rel = f["path"] or ""
            if f["binary"] or f["rename"] or f["created"] or f["deleted"] or f["old"] != rel:
                return "filekind"
            if not rel.startswith("sqlglot/") or not rel.endswith(".py"):
                return "nonsrc"
            rec = self._patched.get(rel)
            if rec is None:
                return "norec"
            old_tree, new_tree, new_src = rec
            if same_tree(old_tree, new_tree):
                continue
            if leaf_diffs(old_tree, new_tree) is None:
                return "structural"
            headers = def_headers(old_tree)
            out = lines.setdefault(rel, set())
            touched = touched_old_lines(f)
            if not any(self.innermost(rel, ln)[0] or self.lambda_range(rel, ln) for ln in touched):
                owners = self.argt_edit(old_tree, new_tree)
                if owners:
                    # arg_types literals: behaviour changes only where the class's dict (or the
                    # required_args derived from it) is read; the map records those reads.
                    for c in owners:
                        lines.setdefault(f"@argt/{c}", set()).add(1)
                    overlay[rel] = new_src
                    continue
                # Class constants: behaviour changes only where they are read at runtime.
                sink: dict = {}
                if self.value_edit(old_tree, new_tree, rel, sink) is None or not sink.get("lines"):
                    return "classbody"
                for f2, lns in sink["lines"].items():
                    t2 = self._tree(f2)
                    if t2 is None:
                        return "classbody-src"
                    dst = lines.setdefault(f2, set())
                    for ln in lns:
                        span = statement_span(t2, ln) or (ln, ln)
                        dst.update(range(span[0], span[1] + 1))
                keep |= sink.get("keep", set())
                overlay[rel] = new_src
                continue
            diffs = leaf_diffs(old_tree, new_tree)
            dflt: dict[int, tuple[int, int]] = {}  # id(node under a default) -> function range
            for n in walk(old_tree):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    for d in list(n.args.defaults) + [x for x in n.args.kw_defaults if x is not None]:
                        for m in walk(d):
                            dflt[id(m)] = (n.lineno, n.end_lineno or n.lineno)
            for ln in touched:
                fn, _ = self.innermost(rel, ln)
                lam = self.lambda_range(rel, ln)
                if fn and ln in headers and not (lam and lam[0] >= fn[0]):
                    # A constant default: it changes only calls of this function, whose body
                    # (or, for a nested def, its def statement) then runs.
                    on_line = [d for d in diffs if getattr(d[1], "lineno", None) == ln]
                    if any(d[0] == "op" for d in diffs) or not on_line or any(id(d[1]) not in dflt for d in on_line):
                        return "header"
                    if self.in_import_fn(rel, ln):
                        return "header-import"
                    for d in on_line:
                        a, b = dflt[id(d[1])]
                        out.update(range(a, b + 1))
                    continue
                if lam and (fn is None or lam[0] >= fn[0]):
                    # A lambda body runs only when the lambda is called (class tables are built
                    # at import, but tests never re-run class bodies).
                    if fn is None and not lambda_body_only(old_tree, new_tree):
                        return "lambda-table"
                    if fn is not None and (self.in_import_fn(rel, ln) or self.ran_at_import(rel, old_tree, ln)):
                        return "lambda-import"
                    out.update(range(lam[0], lam[1] + 1))
                    continue
                if fn is None:
                    return "classbody"
                if ln in headers:
                    return "header"
                if self.in_import_fn(rel, ln) or self.ran_at_import(rel, old_tree, ln):
                    return "import"
                span = statement_span(old_tree, ln) or (ln, ln)
                out.update(range(span[0], span[1] + 1))
            overlay[rel] = new_src
        if not overlay:
            return "nothing"
        return lines, overlay, keep

    def argt_edit(self, old_tree, new_tree) -> list[str] | None:
        """Classes whose `arg_types = {...}` literal is the only thing this file's edit changes,
        when every one of them is an expression class whose arg_types nothing reads at import
        (map "argt_import": a read outside every test, e.g. while a test module imports) and no
        source names explicitly (`C.arg_types` / `C.required_args`: copies into other classes,
        module-level sets, reads the map can't attribute). None otherwise."""
        def assigns(tree):
            out = {}
            for n in walk(tree):
                if isinstance(n, ast.ClassDef):
                    for st in n.body:
                        if (isinstance(st, ast.Assign) and len(st.targets) == 1
                                and isinstance(st.targets[0], ast.Name) and st.targets[0].id == "arg_types"):
                            out.setdefault(n.name, []).append(st)
                        elif (isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name)
                              and st.target.id == "arg_types" and st.value is not None):
                            out.setdefault(n.name, []).append(st)
            return out

        oa, na = assigns(old_tree), assigns(new_tree)
        if set(oa) != set(na) or any(len(oa[c]) != 1 or len(na[c]) != 1 for c in oa):
            return None
        changed = [c for c in oa if ast.dump(oa[c][0].value) != ast.dump(na[c][0].value)]
        if not changed:
            return None
        for c in changed:
            v = na[c][0].value
            if not isinstance(v, ast.Dict) or not all(isinstance(x, ast.Constant) or x is None for x in v.keys):
                return None
        # Nothing else in the module may differ: blank out the changed values and compare.
        def masked(tree, table):
            import copy
            t2 = copy.deepcopy(tree)
            ids = {(st.lineno, st.col_offset) for c in changed for st in table[c]}
            for n in walk(t2):
                if isinstance(n, (ast.Assign, ast.AnnAssign)) and (n.lineno, n.col_offset) in ids:
                    n.value = ast.Constant(value=None)
            return ast.dump(t2)
        if masked(old_tree, oa) != masked(new_tree, na):
            return None
        data = self._stmt_data()
        if not data or "argt_import" not in data:
            return None
        imported = set(data["argt_import"])
        named = self.argt_named()
        bases = self.index.get("class_bases", {})
        for c in changed:
            if c in imported or c in named:
                return None
            seen, stack = set(), [c]
            while stack:
                k = stack.pop()
                if k in seen:
                    continue
                seen.add(k)
                stack.extend(bases.get(k, []))
            if "Expr" not in seen:
                return None
        return changed

    def argt_named(self) -> set[str]:
        """Names X written as `X.arg_types` or `X.required_args` anywhere in sqlglot/ (once)."""
        got = self.__dict__.get("_argt_named")
        if got is None:
            import re
            pat = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.(?:arg_types|required_args)\b")
            got = set()
            for p in (SRC / "sqlglot").rglob("*.py"):
                try:
                    got.update(pat.findall(p.read_text(encoding="utf-8", errors="replace")))
                except OSError:
                    pass
            self._argt_named = got
        return got

    def _tree(self, rel: str):
        memo = self.__dict__.setdefault("_tree_memo", {})
        if rel not in memo:
            src = self.source(rel)
            try:
                memo[rel] = ast.parse(src) if src is not None else None
            except SyntaxError:
                memo[rel] = None
        return memo[rel]

    def _stmt_data(self):
        d = self.__dict__.get("_sm")
        if d is None:
            import marshal
            import zlib
            if not STMT_MAP.exists():
                d = {}
            else:
                d = marshal.loads(zlib.decompress(STMT_MAP.read_bytes()))
            self._sm = d
        return d

    def stmt_entry(self, tid: str):
        memo = self.__dict__.setdefault("_sm_memo", {})
        if tid in memo:
            return memo[tid]
        import marshal
        import zlib
        blob = self._stmt_data().get("tests", {}).get(tid)
        e = None
        if blob is not None:
            from itertools import accumulate
            e = marshal.loads(zlib.decompress(blob))
            e["lines"] = list(accumulate(e["d"]))  # delta-encoded; prefix sums in C
        memo[tid] = e
        return e

    def _probe_mod(self):
        """tools/stmt_probe.py, loaded into this process with sqlglot and the test helpers (once):
        each probe then forks from here instead of starting and importing everything again."""
        mod = self.__dict__.get("_probe")
        if mod is None:
            import importlib.util
            spec = importlib.util.spec_from_file_location("stmt_probe", STMT_PROBE)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.prepare(SRC.resolve())
            self._probe = mod
        return mod

    def stmt_probe(self, out: set[int], files: list[dict]) -> tuple[set[int], str]:
        loc = self.local_lines(files)
        if isinstance(loc, str):
            return set(), "nl-" + loc
        changed, overlay, keep = loc
        data = self._stmt_data()
        if not data:
            return set(), ""
        fids = data["files"]
        enc = sorted(fids[rel] * 100000 + ln for rel, lns in changed.items() if rel in fids for ln in lns)
        num_to_test = {t["number"]: t for t in self.tests}
        impact = set()
        for rel, lns in changed.items():
            impact |= self.cov(rel, lns)
        tpcds = self.by_id.get(TPCDS_TEST)
        static, jobs = set(), []
        tally = {"exempt": 0, "noent": 0, "zero-in-impact": 0, "setup": 0}
        for n in sorted(out):
            t = num_to_test[n]
            if n == tpcds or t["file"] in NO_STMT_PROBE or n in keep:
                tally["exempt"] += 1
                continue
            e = self.stmt_entry(t["id"])
            if e is None or not e["ok"]:
                tally["noent"] += 1
                continue
            lines, masks, mids = e["lines"], e["masks"], e["m"]
            mask = 0
            for x in enc:
                i = bisect.bisect_left(lines, x)
                if i < len(lines) and lines[i] == x:
                    mask |= masks[mids[i]]
            ctx = e["ctx"]
            if not mask:
                if n not in impact:
                    static.add(n)
                else:
                    tally["zero-in-impact"] += 1
                continue
            full, cases, t_need, t_all = set(), {}, 0.0, 0.0
            setup = False
            for i, (stmt, cf, ck, secs) in enumerate(ctx):
                t_all += secs
                if not (mask >> i) & 1:
                    continue
                t_need += secs
                if stmt < 0:
                    setup = True
                elif cf is None:
                    full.add(stmt)
                else:
                    cases.setdefault(stmt, {}).setdefault(cf, []).append(ck)
            if setup:
                tally["setup"] += 1
                continue
            for stmt in full:
                cases.pop(stmt, None)
            # kept-for-binding statements: their statement-level time
            scale = t["seconds"] / t_all if t_all > 0 else 1.0
            est = t_need * scale
            jobs.append((PROBE_PER_TEST_S + PROBE_SAFETY * est, n, {"id": t["id"], "full": sorted(full),
                                  "exposure": bin(mask).count("1"),
                                  "cases": {str(k): v for k, v in cases.items()}}))
        if not jobs:
            return static, "nojobs:" + ",".join(f"{k}={v}" for k, v in tally.items() if v)
        ms = {x: round(num_to_test[x]["seconds"] * 1000) for x in out}

        def cost(sel):
            if not sel:
                return 0.0
            ws = split(set(sel), ms)
            return (max(sum(ms[x] for x in w) for w in ws) + WORKER_MS * len(ws)) / 1000

        base = out - static
        c0 = cost(base)
        jobs.sort(key=lambda j: (j[0] / max(ms[j[1]], 1), j[0]))
        best, best_gain, acc = 0, 0.0, 0.0
        chosen = set()
        for i, (est, n, _job) in enumerate(jobs, 1):
            acc += est
            chosen.add(n)
            gain = c0 - cost(base - chosen) - PROBE_RATIO * (PROBE_START_S + acc)
            if gain > best_gain:
                best, best_gain = i, gain
        if os.environ.get("PROBE_VERBOSE"):
            for est, n, job in jobs:
                log(f"  job {num_to_test[n]['id']} est={est:.2f} ms={ms[n]} exp={job['exposure']} "
                    f"full={len(job['full'])} cases={sum(len(v) for c in job['cases'].values() for v in c.values())}")
            log(f"  base c0={c0:.1f} best={best} gain={best_gain:.2f} static={len(static)}")
        if best == 0:
            return static, "static" if static else "skip"
        pick = jobs[:best]
        cpu_est = PROBE_START_S + sum(j[0] for j in pick)
        order = sorted(pick, key=lambda j: -j[2]["exposure"])  # likeliest to break first
        job = {"overlay": overlay, "tests": [j[2] for j in order], "failfast": True,
               "trees": {rel: self._patched[rel][:2] for rel in overlay if rel in self._patched}}
        raw_gain = best_gain + PROBE_RATIO * cpu_est
        budget = max(1.5 * cpu_est + 0.3, min(0.8 * raw_gain, 3 * cpu_est + 0.5))
        allow = probe_allowance()
        if allow < 1.5 * cpu_est + 0.3:
            return static, "time"
        budget = min(budget, allow)
        res: dict = {}
        cpu = 0.0
        try:
            probe = self._probe_mod()
            res, cpu = probe.run_forked(job, budget)
        except Exception as e:  # noqa: BLE001
            log(f"stmt probe failed: {type(e).__name__}: {e}")
        passed = {n for _est, n, job in pick if res.get(job["id"]) is True}
        real = c0 - cost(base - passed)
        log(f"stmt probe est={cpu_est:.2f}s cpu={cpu:.2f}s ran={len(res)}/{len(pick)} "
            f"pass={len(passed)} gain_est={best_gain:.1f} gain={real:.1f} n={len(out)} c0={c0:.1f}")
        return static | passed, f"probe{len(pick)}"


# -- fast tree helpers. ast.walk/ast.dump are pure Python and were most of the runtime CPU.

_AST = ast.AST
_WALK_MEMO: dict[int, tuple] = {}   # id(tree) -> (tree, [nodes in ast.walk order])
_LEAF_MEMO: dict[tuple, tuple] = {}  # (id(a), id(b)) -> (a, b, leaf_diffs(a, b))


def _iter_walk(node):
    """ast.walk, same breadth-first order, without the iter_fields/iter_child_nodes generators."""
    todo = [node]
    i = 0
    while i < len(todo):
        n = todo[i]
        i += 1
        for f in n._fields:
            v = getattr(n, f, None)
            if isinstance(v, _AST):
                todo.append(v)
            elif v.__class__ is list:
                todo.extend([x for x in v if isinstance(x, _AST)])
        yield n


def nodes(tree) -> list:
    """Every node of tree in ast.walk order, computed once per tree object."""
    hit = _WALK_MEMO.get(id(tree))
    if hit is None or hit[0] is not tree:
        hit = _WALK_MEMO[id(tree)] = (tree, list(_iter_walk(tree)))
    return hit[1]


def walk(tree):
    """Drop-in for ast.walk: cached for whole modules (walked many times per commit)."""
    return nodes(tree) if isinstance(tree, ast.Module) else _iter_walk(tree)


def _forget(tree) -> None:
    """A tree is about to change in place: drop everything cached about it."""
    _WALK_MEMO.pop(id(tree), None)
    _LEAF_MEMO.clear()


def clear_tree_caches() -> None:
    _WALK_MEMO.clear()
    _LEAF_MEMO.clear()


_BLOCK = (ast.stmt, ast.excepthandler, ast.match_case)


def _statements(tree) -> list:
    """tree plus every statement, handler and match case in it: the only places a def or class
    (and so a docstring) can sit, without visiting any expression node."""
    out = [tree]
    i = 0
    while i < len(out):
        n = out[i]
        i += 1
        for f in n._fields:
            v = getattr(n, f, None)
            if v.__class__ is list and v and isinstance(v[0], _BLOCK):
                out.extend(v)
    return out


def same_tree(a, b) -> bool:
    """ast.dump(a) == ast.dump(b), without building the strings, stopping at the first difference."""
    stack = [(a, b)]
    pop, push = stack.pop, stack.append
    while stack:
        x, y = pop()
        if x.__class__ is not y.__class__:
            return False
        if isinstance(x, _AST):
            for f in x._fields:
                push((getattr(x, f, None), getattr(y, f, None)))
        elif x.__class__ is list:
            if len(x) != len(y):
                return False
            stack.extend(zip(x, y))
        elif x != y:
            # Same class, so this matches dump's repr comparison (parsed constants are never nan
            # or -0.0; if they were, this errs towards "different", which selects more).
            return False
    return True

SAFE_BUILTIN_BASES = {"object", "Generic", "ABC", "Protocol", "Exception", "ValueError", "TypeError",
                      "KeyError", "RuntimeError"}
SWAP_OPS = (ast.cmpop, ast.operator, ast.unaryop, ast.boolop)


def leaf_diffs(a, b) -> list[tuple] | None:
    """Memoised per tree pair (callers only read the result; strip_docstrings clears it)."""
    key = (id(a), id(b))
    hit = _LEAF_MEMO.get(key)
    if hit is None or hit[0] is not a or hit[1] is not b:
        hit = _LEAF_MEMO[key] = (a, b, _leaf_diffs(a, b))
    return hit[2]


def _leaf_diffs(a, b) -> list[tuple] | None:
    """Where two same-shaped trees differ: ("const", old, new) for Constant values, ("cmp", old_compare,
    new_compare, i) for the i-th comparison operator, ("op", old, new) for other operators.
    None when they differ in any other way (shape, names, attributes...)."""
    wa, wb = nodes(a), nodes(b)
    if len(wa) != len(wb):
        return None
    out: list[tuple] = []
    for x, y in zip(wa, wb):
        if type(x) is not type(y):
            if isinstance(x, ast.cmpop) and isinstance(y, ast.cmpop):
                continue  # recorded on the Compare (operator nodes are shared singletons)
            if any(isinstance(x, k) and isinstance(y, k) for k in SWAP_OPS[1:]):
                out.append(("op", x, y))
                continue
            return None
        if isinstance(x, ast.Compare):
            for i, (ox, oy) in enumerate(zip(x.ops, y.ops)):
                if type(ox) is not type(oy):
                    out.append(("cmp", x, y, i))
        for field, vx in ast.iter_fields(x):
            vy = getattr(y, field, None)
            if isinstance(vx, ast.AST):
                continue
            if isinstance(vx, list):
                if not isinstance(vy, list) or len(vx) != len(vy):
                    return None
                if [v for v in vx if not isinstance(v, ast.AST)] != [v for v in vy if not isinstance(v, ast.AST)]:
                    return None
                continue
            if type(vx) is not type(vy) or vx != vy:
                if isinstance(x, ast.Constant) and field == "value":
                    out.append(("const", x, y))
                    continue
                return None
    return out


CONST_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def scope_signature(src: str, rel: str) -> list:
    """Every scope's kind, name, generator/async flags and each symbol's binding kind: two sources
    with equal signatures resolve every name the same way (no new local, global or free name)."""
    import symtable

    out = []

    def walk(tab) -> None:
        syms = sorted((s.get_name(), s.is_local(), s.is_global(), s.is_free(), s.is_parameter(),
                       s.is_nonlocal(), s.is_imported(), s.is_assigned()) for s in tab.get_symbols())
        flags = (tab.get_type(), tab.get_name(), getattr(tab, "is_nested", lambda: None)())
        out.append((flags, syms))
        for ch in tab.get_children():
            walk(ch)

    walk(symtable.symtable(src, rel, "exec"))
    return out


def statement_span(tree, line: int) -> tuple[int, int] | None:
    """Lines of the innermost statement whose own text holds `line`: a simple statement's whole
    range, or a compound statement's header (up to its first body statement). None for def/class
    headers and anything else."""
    best = None
    for node in walk(tree):
        if not isinstance(node, ast.stmt) or not (node.lineno <= line <= (node.end_lineno or node.lineno)):
            continue
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and getattr(body[0], "lineno", None):
            head = (node.lineno, body[0].lineno - 1)
        elif isinstance(body, list):
            continue
        else:
            head = (node.lineno, node.end_lineno or node.lineno)
        if head[0] <= line <= head[1] and (best is None or head[0] >= best[1][0]):
            best = (node, head)
    if best is None or isinstance(best[0], (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return None
    return best[1]


def lambda_body_only(old_tree, new_tree) -> bool:
    """Every difference between the trees is a leaf (constant, operator, comparison) inside the
    body of a lambda: the table entry and its key are untouched, only what the call computes."""
    diffs = leaf_diffs(old_tree, new_tree)
    # Operator diffs carry shared singleton nodes (ast.And()), whose identity says nothing about
    # where they sit: only constants and comparisons (unique nodes) are placed.
    if not diffs or any(d[0] == "op" for d in diffs):
        return False
    inside: set[int] = set()
    for node in walk(old_tree):
        if isinstance(node, ast.Lambda):
            inside |= {id(n) for n in walk(node.body)}
    return all(id(d[1]) in inside for d in diffs)


def call_name(c: ast.Call) -> str | None:
    fn = c.func
    return fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None


def def_headers(tree) -> dict[int, tuple[int, int]]:
    """Old line -> (start, end) of the function whose header (decorators, signature, defaults) holds it."""
    out: dict[int, tuple[int, int]] = {}
    if tree is None:
        return out
    for n in walk(tree):  # outer first, so nested defs overwrite
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = min([n.lineno] + [d.lineno for d in n.decorator_list])
            # strip_docstrings may leave a bare Pass with no position as the only statement.
            first = next((s.lineno for s in n.body if getattr(s, "lineno", None)), n.end_lineno + 1)
            for ln in range(start, first):
                out[ln] = (start, n.end_lineno)
    return out


def class_bound(tree) -> set[tuple[str, str]]:
    """(class, attribute) for every name a class body binds, at any nesting."""
    out: set[tuple[str, str]] = set()
    if tree is None:
        return out
    for c in walk(tree):
        if isinstance(c, ast.ClassDef):
            for s in c.body:
                if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    out.add((c.name, s.name))
                for tg in (s.targets if isinstance(s, ast.Assign) else [s.target] if isinstance(s, ast.AnnAssign) else []):
                    if isinstance(tg, ast.Name):
                        out.add((c.name, tg.id))
    return out


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
                names |= {e.value.lower() for e in walk(st.value)
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
                for n in walk(t):
                    if isinstance(n, ast.Name):
                        out.add(n.id)
    return out


def def_names(tree, nested: bool = True) -> set[tuple[str, str]]:
    """(enclosing class, function name) of every def, at any depth; with nested=False only module
    functions and class methods (not defs inside a function body)."""
    out: set[tuple[str, str]] = set()
    if tree is None:
        return out

    def visit(node, cls: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, child.name)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.add((cls, child.name))
                if nested:
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
    _forget(tree)
    for node in _statements(tree):
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
    for node in walk(old_tree):
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
        lambdas = [(n.lineno, n.end_lineno) for n in walk(node) if isinstance(n, ast.Lambda)]
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


RUN_LIMIT_S = float(os.environ.get("RUN_LIMIT_S", "180"))  # wall time for the whole batch (4.5 min in the final)
T0 = __import__("time").monotonic()
LEFT = {"commits": 1}


def probe_allowance() -> float:
    """Wall seconds a probe may take now: what is left of 60% of the batch's time limit (the
    limit scales with the batch: 3 min for ~30 commits, 4.5 for ~45), keeping 1 s for each
    commit still to answer."""
    import time
    limit = 0.6 * RUN_LIMIT_S * (1.5 if LEFT.get("total", 0) > 37 else 1.0)
    return limit - (time.monotonic() - T0) - 1.0 * LEFT["commits"]


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    everything = set(ms)
    try:
        sel = Selector(tests)
    except Exception as e:  # map unreadable: run everything on every commit rather than print nothing
        log(f"selector init failed: {type(e).__name__}: {e}")
        sel = None
    patches = sorted((DATA / "commits").glob("*.patch"))
    LEFT["total"] = len(patches)
    for i, patch in enumerate(patches):
        LEFT["commits"] = len(patches) - i
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


def entry() -> None:
    if sys.argv[1:] == ["--bake"]:
        bake()
    else:
        main()


if __name__ == "__main__":
    entry()
