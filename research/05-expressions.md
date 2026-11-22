# 05: Expression classes (`sqlglot/expressions/*.py`) and class-attribute edits

Scope: what reads `arg_types`, `required_args`, `is_var_len_args`, `_sql_names`, `key`, and the class
hierarchy at run time. Which tests break when one of these changes, how `selector.py` handles such an
edit today, and a tighter rule that does not lose recall. Everything here comes from static reading
and read-only queries on `maps/impact.json.gz`. The refs I measured were approximated with `grep -w`.

## Answer: Func classes registered only by name

**Question.** Many Func subclasses (in math.py, string.py, and so on) are registered by name, with no
explicit references. For a class-body edit to one of them, does the current
`import_time_change` + `class_tests` select the tests that use it?

**Answer: no.** The selection is often almost empty, yet still non-empty, so the `module-level-unmapped`
→ all-tests fallback (`selector.py:495-496`) never fires. That produces a miss. Checked against the
current `selector.py` (805 lines); the path is unchanged.

For a class-body line, `sqlglot_change` calls `import_time_change(rel, ln)` (`selector.py:494`), which
returns the union of three sets.

1. **`file_cov("sqlglot/expressions/string.py")`.** This is ∅, and the same holds for `math.py`. Both
   modules are class bodies only, run at import with no test context.
2. **`name_tests({"string"})`.** "string" is a module stem, not a dialect, so this matches coincidental
   tests that hold the literal `"string"`:
   - `test_snowflake::test_try_cast`
   - `test_lineage::test_unpivot_multi_column`
   - `test_schema::test_schema_add_table_with_and_without_mapping`
3. **`class_tests(cls)`.** The refs (`build_index.py:285-306`) are:
   - the class's own definition line;
   - typing, `FUNCTIONS` and `TRANSFORMS` table rows. These run at import, so their coverage is empty
     or credits whichever test imported the module first (`parsers/clickhouse.py:370` →
     `test_optimizer::*`);
   - a `<lower>_sql` method, if one exists.
   - What is missing: the actual path is `FUNCTION_BY_NAME` → `parser.py:402` → `_parse_function_call`
     → `validate_expression` (`parser.py:7698`) → `function_fallback_sql`. That path is generic and
     shared by every function, so no line on it names the class.

**Measured over all 542 Func classes** in string, math, temporal, aggregate, array, json and
functions.py:
- For each class I took the current selection (grep-approximated refs, plus `file_cov`, plus the
  stem-string noise) and the tests whose Python strings call one of its SQL names. The SQL names are
  `_sql_names`, or the snake-case class name, plus dialect parser table keys. A call means
  `\bname\s*\(`, matched case-insensitively.
- **320 classes (59%) miss at least one such test** (median 2 tests per class).
- 165 are fully covered, 51 have no calling test, and 6 come back ∅, which falls back to all tests (safe).

Examples:

| edit | current selection | missed |
|---|---|---|
| `Ascii` (`string.py:11`, `arg_types` → `{"this":True,"expression":True}`) | 3 tests, all "string" noise | `test_dialect::test_ascii`, `test_dialect::test_is_ascii` |
| `Soundex` | 20 tests | `test_dialect::test_soundex`, `test_snowflake::test_snowflake` |
| `Levenshtein` (`"ins_cost": True`) | 42 tests | `test_sqlite::test_sqlite` (`EDITDIST3(`), `test_clickhouse::test_clickhouse` |
| `Sqrt` | (not recorded) | 2 tests |
| `Cbrt` | (not recorded) | 1 test |

A required-arg flip (False→True), a key reorder, an `is_var_len_args` flip, or a `_sql_names` change
breaks exactly the tests that parse or generate that name. That is 300 s per missed set.

**Caveat.** Fixtures such as `optimizer/annotate_functions.sql` are not in `test_strings`, and
`build_map.py:162` drops strings longer than 200 characters. The true miss set is therefore at least
this large.

### C0. Minimal exact fix (independent of C1–C4)

The fix is a build-time map from SQL function name to Func class, plus the tests whose strings or
fixtures call that name. It goes in two places:
- **`tools/build_index.py`:** add the `sql_names_of` and `scan_tests` helpers from C1 below, plus the
  `calls` loop (dict tables: `"NAME": ...exp.Cls...`). Store only `"calls"` (class → names) and
  `"call_tests"` (name → test ids, `"*"` = a fixture with no known reader).
- **`selector.py`:** in `import_time_change`, after `hit |= self.class_tests(cls)` (`selector.py:357-358`):

```python
        if cls and rel.startswith("sqlglot/expressions/"):
            # Funcs are registered by name (FUNCTION_BY_NAME, dialect FUNCTIONS rows) and printed by the
            # generic function_fallback_sql: no line names the class, so use the SQL names instead.
            subs, todo, seen = self.index.get("subs", {}), [cls], set()
            while todo:
                n = todo.pop()
                if n in seen:
                    continue
                seen.add(n)
                todo.extend(subs.get(n, []))
                if len(seen) > 200:
                    return set(self.all)
            words = set()
            for n in seen:
                words |= set(self.index.get("calls", {}).get(n, ()))
            if "calls" not in self.index:
                return set(self.all)  # index missing: cannot see by-name users
            for w in words:
                tids = self.index.get("call_tests", {}).get(w, ())
                if "*" in tids:
                    return set(self.all)
                hit |= {self.by_id[t] for t in tids if t in self.by_id}
```

Also skip `name_tests(words)` when `rel` does not start with `DIALECT_DIRS`, so that an empty real
signal falls back to all tests rather than to "string" noise. The patch may change `_sql_names`
itself, so `python_change` should also pass both the old and the new names. In C2 these come from
`sql_names_of(old_c) | sql_names_of(new_c)`.

## (a) Findings

**F1. There is no metaclass.**
- `@trait` does nothing outside mypyc (`helper.py:24-25`).
- `Expr.__init_subclass__` (`core.py:107-113`) sets `key = __name__.lower()`, overwriting any `key = ...`
  in the class body. It also sets `required_args = {k for k, v in arg_types.items() if v}`.
- So a class-body edit reaches only that class and its subclasses. Two exceptions are built at import
  over every class (`expressions/__init__.py:49-51`):
  - `FUNCTION_BY_NAME` maps every `sql_names()` to its class. It is built in alphabetical
    `getmembers` order, so on a collision the later class wins.
  - `EXPR_CLASSES` maps `key` to its class.

**F2. Constructing an expression does not validate it.**
- `Expr.__init__` (`core.py:116-128`) only stores the args and sets parents.
- `error_messages` (`core.py:1314-1342`) checks three things:
  - (i) under `UNITTEST`, any key not in `arg_types` raises `TypeError`, even when its value is `None`;
  - (ii) a required arg that is `None` or `[]` is an error, while `False` and `0` pass;
  - (iii) a `Func` with more args than `arg_types` is an error unless `is_var_len_args` is set.
- Only two callers run it:
  - the parser's `validate_expression` (`parser.py:2199-2207`), through `self.expression()`
    (`parser.py:2289-2300`) and `_parse_function_call` (`parser.py:7698`, also `8478`, `8530`, `8665`, `9004`);
  - `exp.func()` (`builders.py:1027-1051`).
- The generator, optimizer, copy, and serde never validate. Serde's `_load` builds `cls()` and then
  calls `.set()` (`serde.py:107-124`). It depends only on the class qualname (`serde.py:47-51`).

**F3. Other readers of these attributes:**
- `Func.from_arg_list` fills args positionally in `arg_types` order and splits at `var_len_arg_key`
  when `is_var_len_args` is set (`core.py:1677-1689`).
- `sql_names()` falls back to `camel_to_snake_case(name)` (`core.py:1692-1699`, `helper.py:160`).
  `_sql_names` is inherited by subclasses.
- The parser's default `FUNCTIONS` comes from `FUNCTION_BY_NAME` (`parser.py:402`).
- Generator dispatch is `<key>_sql` looked up via `EXPR_CLASSES` (`generator.py:73-88`).
- `function_fallback_sql` walks `arg_types` in order and prints `sql_name()` (`generator.py:5036-5052`).
  It is the path for every `Func` without a `<key>_sql` method or a TRANSFORMS entry.
- `simplify.gen` walks `arg_types` in order (`simplify.py:1961-1966`). `test_optimizer::test_gen` runs it
  over `ALL_FUNCTIONS` (`tests/test_optimizer.py:5114`).
- One import-time copy of a value exists: `UNMERGABLE_ARGS = set(exp.Select.arg_types)` (`merge_subqueries.py:123`).
- `__hash__` uses `key` and `_hash_raw_args`, never `arg_types` (`core.py:864-885`).

**F4. Every failure needs an instance of the class or a subclass.** Instances come from four places:
- (A) construction sites inside sqlglot functions;
- (B) by-name factories: `FUNCTION_BY_NAME`, dialect `FUNCTIONS` rows such as
  `"EDITDIST3": exp.Levenshtein.from_arg_list` (`parsers/sqlite.py:110`), and `exp.func("name")`;
- (C) operator token tables;
- (D) test code (`exp.Foo(...)`, `find(exp.Foo)`, `into=`).

What each kind of edit affects:
- **False→True:** validated (A)/(B) sites that leave the arg `None`.
- **True→False:** nothing, except tests that expect a ParseError.
- **A key removed or renamed:** (A)/(B) parsing (check i), plus generation (`<key>_sql` reads the old key).
- **Key order, `is_var_len_args`:** (B) by name, plus fallback generation.
- **`_sql_names`:** tests that call an old or new name, including collisions.
- **Bases:** every `isinstance(x, exp.Base)`.
- **A class renamed or removed:** effectively everything (`key`, `EXPR_CLASSES`, serde, repr, and
  `AttributeError` in import-time tables).

**F5. ktz3.** It flips `TriggerProperties.constraint` from False to True (`ddl.py:82`) and has 0 failing
tests. The only validated construction (`parser.py:2732-2746`) passes `constraint=is_constraint`, a bool
that is never `None`. The tests that can see the edit are that statement's coverage (`parser.py:2733-2745`,
3 tests), `triggerproperties_sql`, and `generator.py:1407-1413`.

**F6. The current selector does not fit these edits.** A class-body edit goes to `import_time_change`
(`selector.py:470-497`), which takes:
- `file_cov(rel)`, the coverage of every function in the module (`selector.py:352-353`):

  | module | tests | seconds |
  |---|---|---|
  | `core.py` | 1540 | 95.8 |
  | `query.py` | 973 | 64.5 |
  | `datatypes.py` | 598 | 60.6 |
  | `functions.py` | 229 | 43.1 |
  | `ddl.py` | 128 | 5.9 |
  | `string.py`, `math.py` | 0 | 0 |

- `name_tests({stem})` on non-dialect words (`selector.py:354-355`). For example, "temporal" matches 167
  tests by coincidence.
- `class_tests` (`selector.py:356-358`).

Measured over all classes, current mean against `class_tests` alone:

| module | current | `class_tests` alone | classes under 10 s with `class_tests` alone |
|---|---|---|---|
| `core.py` | 96 s | 32 s | 51 of 117 |
| `query.py` | 66 s | 18 s | 100 of 146 |
| `functions.py` | 44 s | 6.5 s | 87 of 96 |
| `ddl.py` | 10 s | 5.5 s | 51 of 56 |

`file_cov` adds no recall for an attribute edit (F1).

**F7. `class_tests` refs are mostly import-time table rows.** `build_index.py:101-115` records every token
line that names the class. The coverage of those rows is either empty (`generator.py:826`) or noise from
the first test that lazily imported the module:
- `parsers/clickhouse.py:370` maps to `test_optimizer::test_annotate_funcs` and similar, not to `test_clickhouse`.
- `generators/duckdb.py:2045` maps to `test_optimizer::test_eliminate_joins` and similar.

So Funcs reached only through (B) have no real signal. The `len(names) < 400` cap (`selector.py:308`) also
silently truncates the closure: `Func` has 475 direct subclasses and `Expression` 892. For common classes
(Column, Literal, Select, Cast) `class_tests` is 1200 to 1460 tests, which is inherent because those
instances are everywhere.

## (b) Recall risks and waste

R1 was checked against the map. R2 to R5 are open risks.

**R1. A miss today.** See the answer section at the top: 320 of 542 Func classes, for example
Soundex, Levenshtein and Ascii.

**R2. A `_sql_names` alias collides** with another class, or with a function that used to parse as
Anonymous. `FUNCTION_BY_NAME` changes with no line that names either class.

**R3. A new Func class takes over an existing SQL name.** The touched lines are blank or belong to the
neighbouring class.

**R4. A class removed or renamed** while still referenced in an import-time table causes an
`AttributeError` at dialect import. Only noise coverage selects those tests.

**R5. Test-side construction** of a fallback-generated Func has no coverage link. In addition,
`build_map.py:162` drops test strings longer than 200 characters, so name matching on long SQL can miss.

**Waste.**
- W1: `file_cov(rel)` adds about 30 to 60 s of selected time for most core, query and functions classes.
- W2: `name_tests(stem)` noise.
- W3: random `test_optimizer` tests from the import-time refs.

## (c) Proposed changes

### C1. `tools/build_index.py`: refs inside functions, handlers, call names, test refs

Insert this before `return {` in `build()`, and add `import gzip, re` at the top:

```python
    IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
    CALL = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")
    TABLE_DIRS = ("sqlglot/parser.py", "sqlglot/parsers/", "sqlglot/generator.py", "sqlglot/generators/",
                  "sqlglot/typing/", "sqlglot/dialects/", "sqlglot/tokenizers/", "sqlglot/expressions/")

    def runtime_spans(tree):
        """line -> [start, end] for lines that run inside a function or lambda (widened to the statement)."""
        spans: dict[int, tuple] = {}
        def visit(node, in_fn):
            for ch in ast.iter_child_nodes(node):
                fn = in_fn or isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                if isinstance(ch, ast.Lambda):
                    for ln in range(ch.lineno, ch.end_lineno + 1):
                        spans.setdefault(ln, (ch.lineno, ch.end_lineno))
                elif fn and isinstance(ch, ast.stmt):
                    end = ch.body[0].lineno - 1 if getattr(ch, "body", None) else ch.end_lineno
                    if end - ch.lineno < 40:
                        for ln in range(ch.lineno, max(end, ch.lineno) + 1):
                            spans[ln] = (ch.lineno, max(end, ch.lineno))
                visit(ch, fn)
        visit(tree, False)
        return spans

    refs_fn: dict[str, list] = {}
    refs_mod: dict[str, set] = {}
    for rel, text in texts.items():
        spans = runtime_spans(trees[rel]) if rel in trees else {}
        try:
            for tok in tokenize.generate_tokens(io.StringIO(text).readline):
                if tok.type == tokenize.NAME and tok.string in class_names:
                    ln = tok.start[0]
                    if ln in spans:
                        refs_fn.setdefault(tok.string, []).append([rel, *spans[ln]])
                    elif not rel.startswith(TABLE_DIRS):
                        refs_mod.setdefault(tok.string, set()).add(rel)  # e.g. UNMERGABLE_ARGS
        except Exception:
            continue

    handlers: dict[str, list] = {}   # class -> ranges of callables registered for it in dict tables
    calls: dict[str, set] = {}       # class -> lower-case SQL names that build or print it
    for rel, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for k, v in zip(node.keys, node.values):
                kname = k.attr if isinstance(k, ast.Attribute) else k.id if isinstance(k, ast.Name) else None
                if kname in class_names:
                    for sub in ast.walk(v):
                        if isinstance(sub, ast.Name) and sub.id in fn_ranges:
                            handlers.setdefault(kname, []).extend(fn_ranges[sub.id])
                        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str) and IDENT.fullmatch(sub.value):
                            calls.setdefault(kname, set()).add(sub.value.lower())
                elif isinstance(k, ast.Constant) and isinstance(k.value, str) and IDENT.fullmatch(k.value):
                    for sub in ast.walk(v):
                        n = sub.attr if isinstance(sub, ast.Attribute) else sub.id if isinstance(sub, ast.Name) else None
                        if n in class_names:
                            calls.setdefault(n, set()).add(k.value.lower())
    # Default and explicit SQL names of every class (kept for all classes; cheap).
    for rel, tree in trees.items():
        if not rel.startswith("sqlglot/expressions/"):
            continue
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                calls.setdefault(node.name, set()).update(sql_names_of(node))

    wanted = set().union(*calls.values()) if calls else set()
    test_refs, call_tests = scan_tests(root, class_names, wanted, CALL, IDENT)
```

Add these module-level helpers to the same file. `sql_names_of` is also copied into `selector.py`.

```python
def sql_names_of(cls: ast.ClassDef) -> set[str]:
    out = {re.sub(r"(?<!^)(?=[A-Z])", "_", cls.name).lower()}
    for st in cls.body:
        if isinstance(st, (ast.Assign, ast.AnnAssign)) and any(
                getattr(t, "id", None) == "_sql_names" for t in (st.targets if isinstance(st, ast.Assign) else [st.target])):
            out |= {e.value.lower() for e in ast.walk(st.value) if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return out


def scan_tests(root, class_names, wanted, CALL, IDENT):
    """test id -> classes named in its code; SQL name -> test ids calling it (strings, all lengths, + fixtures)."""
    refs, calls, strings_of = {}, {}, {}
    for path in sorted((root / "tests").rglob("*.py")):
        rel = str(path.relative_to(root))
        try:
            tree = ast.parse(path.read_text())
        except Exception:
            continue
        def harvest(node):
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
            strs = {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
            return names & class_names, strs
        units = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test"):
                units.append((f"{rel}::{node.name}", [node]))
            elif isinstance(node, ast.ClassDef):
                helpers = [i for i in node.body if isinstance(i, ast.FunctionDef) and not i.name.startswith("test")]
                units += [(f"{rel}::{node.name}::{i.name}", [i] + helpers) for i in node.body
                          if isinstance(i, ast.FunctionDef) and i.name.startswith("test")]
        for tid, nodes in units:
            names, strs = set(), set()
            for n in nodes:
                a, b = harvest(n); names |= a; strs |= b
            strings_of[tid] = strs
            for c in names:
                refs.setdefault(c, []).append(tid)
            words = {w.lower() for s in strs for w in CALL.findall(s)} | {s.lower() for s in strs if IDENT.fullmatch(s)}
            for w in words & wanted:
                calls.setdefault(w, set()).add(tid)
    for p in sorted((root / "tests" / "fixtures").rglob("*")):
        if not p.is_file() or p.suffix not in (".sql", ".json", ".csv", ".txt", ".py"):
            continue
        words = {w.lower() for w in CALL.findall(p.read_text(errors="ignore"))} & wanted
        if not words:
            continue
        users = {t for t, ss in strings_of.items() if any(p.stem in s for s in ss)} or \
                {t for t, ss in strings_of.items() if any(p.parent.name in s for s in ss)} or {"*"}
        for w in words:
            calls.setdefault(w, set()).update(users)
    return refs, {k: sorted(v) for k, v in calls.items()}
```

Return the new keys: `"refs_fn": refs_fn, "refs_mod": {k: sorted(v) ...}, "handlers": handlers,
"calls": {k: sorted(v) ...}, "test_refs": test_refs, "call_tests": call_tests`. The old `refs` key stays
for the dialect rule. Build CPU is not charged (Dockerfile line 14, and `tests/` is fetched by
`fetch_src.py`).

### C2. `selector.py`: an expression-class rule in place of `import_time_change` for `expressions/*.py`

In `sqlglot_change`, replace the line `hit = self.import_time_change(rel, ln)` (`selector.py:494`) with:

```python
                hit = None
                if rel.startswith("sqlglot/expressions/") and (cls_name := self.top_class(rel, ln)):
                    hit = self.expr_class_change(cls_name, old_tree, new_tree) or None
                if hit is None:
                    hit = self.import_time_change(rel, ln)
```

Add to `Selector`, and add `sql_names_of` from C1 as a module function:

```python
    def expr_class_change(self, cls: str, old_tree, new_tree) -> set[int] | None:
        """Class-body edit of an Expr class (arg_types, _sql_names, is_var_len_args, flags, bases).
        It reaches only instances of the class or a subclass, which come from construction sites in
        functions, by-name factories (SQL names), handler tables, or test code. None = use the broad rule."""
        ix = self.index
        if "refs_fn" not in ix:
            return None
        old_c, new_c = find_class(old_tree, cls), find_class(new_tree, cls)
        if old_c is None or new_c is None:
            return set(self.all)  # renamed/removed: key, EXPR_CLASSES, import-time tables
        names, todo, subs = set(), [cls], ix.get("subs", {})
        while todo:
            n = todo.pop()
            if n not in names:
                names.add(n)
                todo.extend(subs.get(n, []))
            if len(names) > 200:
                return None  # Func / Expression / Condition: everything
        out: set[int] = set()
        words = sql_names_of(old_c) | sql_names_of(new_c)
        for n in names:
            for rel, s, e in ix["refs_fn"].get(n, []) + ix.get("handlers", {}).get(n, []):
                out |= self.cov(rel, range(s, e + 1))
            for rel in ix.get("refs_mod", {}).get(n, []):
                out |= self.file_cov(rel)
            for rel, s, e in ix.get("fns", {}).get(f"{n.lower()}_sql", []):
                out |= self.cov(rel, range(s, e + 1))
            out |= {self.by_id[t] for t in ix.get("test_refs", {}).get(n, []) if t in self.by_id}
            words |= set(ix.get("calls", {}).get(n, []))
        for w in words:
            tids = ix.get("call_tests", {}).get(w, [])
            if "*" in tids:
                return set(self.all)
            out |= {self.by_id[t] for t in tids if t in self.by_id}
        old_b = {getattr(b, "attr", getattr(b, "id", None)) for b in old_c.bases}
        new_b = {getattr(b, "attr", getattr(b, "id", None)) for b in new_c.bases}
        for b in (old_b ^ new_b) - {None}:
            out |= self.class_tests(b)  # isinstance checks on the added/removed base
        return out


def find_class(tree, name):
    for node in (tree.body if tree is not None else []):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None
```

### C3. New top-level classes in `expressions/*.py` (closes R3)

After the `new_names` block (`selector.py:508-513`), add:

```python
        if rel.startswith("sqlglot/expressions/"):
            added = {n.name: n for n in new_tree.body if isinstance(n, ast.ClassDef)} if new_tree else {}
            for n in {n.name for n in old_tree.body if isinstance(n, ast.ClassDef)}:
                added.pop(n, None)
            for name, node in added.items():
                for w in sql_names_of(node):  # FUNCTION_BY_NAME takeover of an existing SQL name
                    out |= {self.by_id[t] for t in self.index.get("call_tests", {}).get(w, []) if t in self.by_id}
                for r, s, e in self.index.get("fns", {}).get(f"{name.lower()}_sql", []):  # dispatch by key
                    out |= self.cov(r, range(s, e + 1))
                if name in self.index.get("refs", {}):  # shadows a same-named class via star imports
                    out |= self.class_tests(name)
            if added:
                reasons.append("new-expr-class")
```

### C4. Small fixes

- **`class_tests` cap.** When the closure reaches 400, return `set(self.all)` rather than a truncated
  set (`selector.py:308`).
- **`name_tests` stems.** Drop the `name_tests(words)` call when `rel` is not under `DIALECT_DIRS`
  (`selector.py:354-355`). Expression module stems are not dialects.

## (d) Ranked recommendations

1. **C0, or C1 + C2.** This fixes R1, R2 and R5, which are real 300 s misses (59% of the Func classes in
   by-name modules), and it costs only a few name lookups.
2. **C2.** This replaces `file_cov(rel)` with a rule built on construction sites, handlers, test refs and
   SQL names.
   - Mean selection falls from 96/66/44 s (core/query/functions) to roughly 32/18/6 s, plus the name
     tests.
   - ktz3 drops from about 134 tests (6.3 s) to about 7 (under 0.5 s).
   - Residual risk: operator classes from token tables in tests that parse but never generate. Their
     `<key>_sql` coverage already exceeds 1000 tests.
3. **Class removed or renamed → all tests (in C2).** This closes R4.
4. **C3 (new class takes over a SQL name).** This closes R3.
5. **C4.** Fix the 400 cap, and drop `name_tests` for non-dialect stems (the "string" and "temporal"
   noise). Without the second change, an empty real signal never reaches the all-tests fallback.
6. **Follow-up.** Have `build_map.py` stop dropping strings over 200 characters (`build_map.py:162`), or
   rely on `scan_tests`, which keeps every length and reads fixtures.
7. **Do not** try to prove statically that a required flip is harmless, as with ktz3's bool. It is
   fragile.
