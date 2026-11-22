# 01 - sqlglot parser class-level tables: how they are consumed and how to select for them

Pin: eb11aec. Paths are relative to `work/sqlglot/` unless they start with `selector.py`, `tools/` or `maps/`.
Nothing was executed except read-only inspection of `maps/impact.json.gz`, `sample/*` and the sqlglot sources.

## (a) Findings

**F1. There is no metaclass or precomputation for parser tables.**
- `Parser` (`sqlglot/parser.py:363`) has no metaclass and no `__init_subclass__`; grep finds neither anywhere in `parser.py` or `parsers/`.
- The only precomputed things are plain class-body statements:
  - `SHOW_TRIE` / `SET_TRIE` (`parser.py:2032-2033`), built from the base `SHOW_PARSERS` / `SET_PARSERS`.
  - `QUERY_MODIFIER_TOKENS = set(QUERY_MODIFIER_PARSERS)` (`parser.py:1667`).
  - Token-set arithmetic such as `TABLE_ALIAS_TOKENS = ID_VAR_TOKENS - {...}` (`:872`), `COMMENT_TABLE_ALIAS_TOKENS` (`:893`), `UPDATE_ALIAS_TOKENS` (`:895`), `WINDOW_ALIAS_TOKENS` (`:1805`) and `FETCH_TOKENS` (`:1811`).
- Dialects must re-derive these by hand:
  - `mysql.py:267-268`, `snowflake.py:890`, `singlestore.py:237`, `duckdb.py:332-333`, `teradata.py:195` and `spark.py:69` redefine `SHOW_TRIE` / `SET_TRIE`.
  - `oracle.py:51` redefines `QUERY_MODIFIER_TOKENS`.
  - `clickhouse.py:500` and `tsql.py:353` override `QUERY_MODIFIER_PARSERS` without redefining the tokens, so those dialects keep the base set.
  - So a table edit can reach a different table (the derived one) under a different name.
- `_Dialect.__new__` (`dialects/dialect.py:306-432`) only picks `klass.parser_class` (`:343`) and builds time/format tries. It never copies or precomputes parser tables.
- Dialect tables are built by spreading the base table: `{**parser.Parser.X, ...}`, for example `mysql.py:259` and `BaseParser.NO_PAREN_FUNCTIONS` (`parsers/base.py:10`). Every dialect parser inherits from `BaseParser` through `Dialect.parser_class = BaseParser` (`dialect.py:950`). A base entry therefore reaches every dialect that spreads it.
- The tables are only read at parse time, by attribute lookup on `self`. Examples:
  - `self.STATEMENT_PARSERS` (`parser.py:2475-2477`)
  - `self.PROPERTY_PARSERS` (`:3002`, `:3018`, `:7756`)
  - `self.RANGE_PARSERS` (`:6412`)
  - `self.NO_PAREN_FUNCTION_PARSERS`, `self.FUNCTION_PARSERS`, `self.FUNCTIONS` (`:7621`, `:7646`, `:7667`)
  - `_find_parser(self.SHOW_PARSERS, self.SHOW_TRIE)` (`:10083`)
- There is no dynamic table access: no `getattr`, `__dict__` or `vars` on parser tables (grep of `parser.py`, `parsers/*.py`, `dialect.py`).

**F2. `FUNCTIONS` builder calls swallow TypeError** (`parser.py:7690-7695`).
- The call is `try: func_builder(args) except TypeError: func_builder(args, dialect=self.dialect)`.
- Changing a lambda's parameter list (`lambda args:` vs `lambda args, dialect:`) changes control flow. Coverage of the lambda body still sees it, because the body runs on the successful call.
- `FUNCTIONS` also starts with `{name: f.from_arg_list for name, f in exp.FUNCTION_BY_NAME.items()}` (`parser.py:402`). Adding a `Func` subclass in `expressions` therefore adds table entries without touching `parser.py`. That is out of scope here, but the `new-def` rule has to cover it.

**F3. Coverage of table lines is empty by construction.**
- Class-body lines run once, at import, outside any test context. In `maps/impact.json.gz`, `parser.py` lines 402, 406, 507 and 1300 map to an empty set, and 1343 maps to 1 test.
- Lambda bodies are real lines. Line 404 has 42 tests, line 410 has 32 and line 1194 has 52.
- The dispatch lines have large sets: 2475 has 1480 tests, 3002 has 137, 6412 has 1350, 7621 has 1383, 7667 has 716 and 10083 has 36.
- Consequence: for any edit to a table line that is not inside a lambda body, `cov()` is useless. The selector falls into `import_time_change` (`selector.py:250-264`).

**F4. What `import_time_change` returns for the base parser is nearly everything.**
- For `sqlglot/parser.py`, `dialect_family()` returns `{rel}` (`selector.py:~187`), because the file is not under `DIALECT_DIRS`.
- `file_cov("sqlglot/parser.py")` is the union of every line's tests. In the map this is 1519 of 1620 tests, 95.7 s of 103 s (my estimate of the best packing: 36.8 s, the same as selecting everything).
- For `parsers/base.py`, `file_cov` is empty (13 lines, 0 tests). `name_tests` gets an empty word set. `class_tests("BaseParser")` hits only import-time lines. So the result is empty, `module-level-unmapped` fires, and the selector runs all tests. Safe, but the same 36.8 s.
- For a dialect file such as `parsers/mysql.py`, the union is `file_cov(family)` (219 tests, about 10 s for mysql alone) plus the name and string matches. That is scoped to the dialect and acceptable.

**F5. Tests that read or mutate parser tables directly** (static grep of `tests/`):
- `tests/test_generator.py:56-76` mutates `SnowflakeParser.FUNCTIONS`.
- `tests/dialects/test_dialect.py:6104-6125` copies `SnowflakeParser.FUNCTIONS` into a custom parser.
- `tests/dialects/test_snowflake.py:5401-5402` reads `CREATABLES` and `DESCRIBE_QUALIFIER_PARSERS`.
- These do not execute a changed entry line in sqlglot. The first two are covered when a dispatch line runs under them (see R4 for the test-source part). A "dispatch coverage" rule has to add them by static reference to be exact.

**F6. Which tests can fail when one table entry changes?** Any test that, during a parse, reaches the dispatch for that table with an input that makes the changed key (or token type) live.
- That means the dialect tests (`validate_identity`, `validate_all`), `test_parser`, `test_transpile`, `test_build`, `test_expressions`, `test_optimizer` and the TPC tests, `test_executor`, `test_lineage`, `test_schema`, `test_diff`, `test_serde`, the fixture-driven identity test, and the F5 tests.
- A value or key change on a lookup that never happens cannot fail anything. For example, `PROPERTY_PARSERS["FOO"]` cannot matter to a test that never parses `FOO`.
- Dispatch-site coverage sizes, my union over every non-assignment reference to the table in `sqlglot/*.py`:
  - Very small (under 50 tests): SHOW_PARSERS 40, SET_PARSERS 45, ANALYZE_*, ALTER_ALTER_PARSERS, `*_OPTIONS`, TRANSACTION_*, `*_FOLLOW_*`, SET_TRIE, SHOW_TRIE.
  - Small (50-300): PROPERTY_PARSERS 227, CONSTRAINT_PARSERS 291, ALTER_PARSERS 66, CREATABLES 189, TYPE_LITERAL_PARSERS 78, QUERY_MODIFIER_TOKENS 94, EXPRESSION_PARSERS 478 (medium).
  - Medium (500-900): FUNCTIONS 746, FUNCTION_PARSERS 847, TABLE_ALIAS_TOKENS 517, TYPE_CONVERTERS 603, NESTED_TYPE_TOKENS 631.
  - Large (over 1200, no gain): TYPE_TOKENS, NO_PAREN_FUNCTIONS 1237, RANGE_PARSERS 1351, COLUMN_OPERATORS, UNARY_PARSERS, PRIMARY_PARSERS, STATEMENT_PARSERS 1480, ID_VAR_TOKENS, PLACEHOLDER_PARSERS.

**F7. Real example of a builder-argument edit (sample `gbav`).**
- It changes `"GROUPS FOR USER": _show_parser("GROUPS FOR USER", target=True)` to `target=False` in `parsers/singlestore.py:213`.
- `_show_parser` is a closure factory (`mysql.py:55-59`). All keys share one `_parse` body, so no coverage line or lambda range identifies the entry.
- The selector falls to `import_time_change` with `file_cov(family)` plus `name_tests({"singlestore"})`. Dispatch coverage of `SHOW_PARSERS` (`parser.py:10083` plus the `singlestore.py`/`mysql.py` uses) is 36-40 tests.
- I use this only as a shape of change, not for tuning.

## (b) Recall risks and cost waste under the current `selector.py`

**R1 (MISS RISK): insertion next to lambda entries selects only the neighbour lambda's tests.**
- `touched_old_lines` (`selector.py:~93`) returns the line before and the line after an insertion.
- For a table statement of 40 lines or more, `changed_statement_lines` skips widening (`selector.py:~447`). If both neighbours are one-line `"X": lambda ...` entries, each touched line is inside a lambda.
- `sqlglot_change` (`selector.py:~311`) then takes the coverage of those neighbour lambdas only, with reason "lambda".
- Shape: a patch adds `"NEWFUNC": lambda args: exp.NewFunc(...)` between two lambda entries, or adds `"NEWFUNC": build_x`. The patch selects the tests that call the neighbour lambdas.
- Existing tests that parse `NEWFUNC(...)` (fixture lines, identity tests, other dialects' tests) previously produced `Anonymous` and can now fail. They are not selected.
- A real patch adds tests in the same commit, which hides this from the sample. The risk is regression-type commits.

**R2 (MISS RISK): key edits on a lambda line.**
- Renaming or re-keying `"ARRAYAGG": lambda ...` to a new key puts the touched line inside the lambda range.
- The selected tests are those that ran the old lambda. Tests that look up the new key are missed.
- The same applies when the key (a string or a `TokenType.X`) is changed on the same line as the lambda header.

**R3 (cost): any non-lambda entry in `parser.py` selects about 1519 tests, 36.8 s.**
- Examples: a `PROPERTY_PARSERS` entry such as `"FOO": lambda self: ...` is fine, but `"FOO": _parse_foo` or `"X": TokenType.Y` is not.
- Dispatch coverage would cost about 8.8 s for `PROPERTY_PARSERS`, about 2 s for `SHOW_PARSERS` and about 27 s for `FUNCTIONS`.

**R4 (MISS RISK, small): tests reading tables from test source.**
- `test_generator.py:56`, `test_dialect.py:6104` and `test_snowflake.py:5401` depend on table content from test code.
- A change to `SnowflakeParser.FUNCTIONS` or `CREATABLES` selects them only through file-level or dialect-name union.

**R5 (cost): `parsers/base.py`.**
- `file_cov` is empty, so any edit hits `module-level-unmapped`, which selects all tests (36.8 s).
- Dispatch coverage would be exact here. For example, `NO_PAREN_FUNCTIONS` additions in `BaseParser` fall to the dispatch of `NO_PAREN_FUNCTIONS`, which is large (1237 tests, no gain).

**R6 (small): tuple/set edits.** An edit adding a token to `ID_VAR_TOKENS` or `TYPE_TOKENS` is selected broadly, which is correct. The hot sets (F6, "Large") have no recoverable cost.

## (c) Proposed changes

### c1. `tools/build_index.py`: add table maps

After `trees` and `fn_ranges` are built, and before `return`, add:

```python
    # --- parser class-level tables (UPPER_CASE assignments in classes of parser.py / parsers/*)
    def stmt_of(node, parents):
        while node in parents and not isinstance(node, ast.stmt):
            node = parents[node]
        return node.lineno if isinstance(node, ast.stmt) else None

    table_ranges: dict[str, list] = {}   # file -> [[start, end, name]] of table assignments
    tables: set[str] = set()
    readers: dict[str, set] = {}         # table -> derived tables that read it in a class body
    for rel, tree in trees.items():
        if not (rel == "sqlglot/parser.py" or rel.startswith("sqlglot/parsers/")):
            continue
        for cls in tree.body:
            if not isinstance(cls, ast.ClassDef):
                continue
            for node in cls.body:
                tgt = node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else getattr(node, "target", None)
                if isinstance(tgt, ast.Name) and tgt.id.isupper() and getattr(node, "value", None) is not None:
                    table_ranges.setdefault(rel, []).append([node.lineno, node.end_lineno, tgt.id])
                    tables.add(tgt.id)
    for rel, tree in trees.items():
        for lo, hi, name in table_ranges.get(rel, []):
            for node in ast.walk(tree):
                if getattr(node, "lineno", 0) and lo <= node.lineno <= hi:
                    r = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
                    if r in tables and r != name:
                        readers.setdefault(r, set()).add(name)

    table_uses: dict[str, list] = {}     # table -> [[file, stmt line, line]] reads outside table assignments
    for p in sorted((root / "tests").rglob("*.py")):
        try:
            rel = str(p.relative_to(root)); trees[rel] = ast.parse(p.read_text(encoding="utf-8"))
        except Exception:
            continue
    for rel, tree in trees.items():
        parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        skip = table_ranges.get(rel, [])
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in tables:
                ln = node.lineno
                if any(a <= ln <= b for a, b, _ in skip):
                    continue
                table_uses.setdefault(node.attr, []).append([rel, stmt_of(node, parents) or ln, ln])
```

Add `"table_ranges": table_ranges, "table_uses": table_uses, "table_readers": {k: sorted(v) for k, v in readers.items()}` to the returned dict.

Notes:
- Table assignments are skipped as "uses", so a dialect's `{**parser.Parser.X}` is not counted as a dispatch.
- Name collisions with generator or dialect attributes of the same name only add sites. That is safe and slightly broader.
- The build must never fail, so keep the existing outer try/except.

### c2. `selector.py`: dispatch coverage and structural table diffs

Add helpers (module level):

```python
import copy

class _StripLambdaBodies(ast.NodeTransformer):
    def visit_Lambda(self, node):
        return ast.Lambda(args=node.args, body=ast.Constant(None))


def class_tables(tree) -> dict:
    out = {}
    for cls in (tree.body if tree else []):
        if isinstance(cls, ast.ClassDef):
            for n in cls.body:
                tgt = n.targets[0] if isinstance(n, ast.Assign) and len(n.targets) == 1 else getattr(n, "target", None)
                if isinstance(tgt, ast.Name) and tgt.id.isupper():
                    out[(cls.name, tgt.id)] = n
    return out


def table_edit_kinds(old_tree, new_tree) -> dict:
    """(class, table) -> "lambda" (only lambda bodies changed) or "structural"."""
    old, new = class_tables(old_tree), class_tables(new_tree)
    out = {}
    for key in old.keys() | new.keys():
        a, b = old.get(key), new.get(key)
        if a is not None and b is not None and ast.dump(a) == ast.dump(b):
            continue
        if a is None or b is None:
            out[key] = "structural"
            continue
        sa = ast.dump(_StripLambdaBodies().visit(copy.deepcopy(a)))
        sb = ast.dump(_StripLambdaBodies().visit(copy.deepcopy(b)))
        out[key] = "lambda" if sa == sb else "structural"
    return out
```

In class `Selector`, add:

```python
    def table_at(self, rel: str, line: int):
        for start, end, name in self.index.get("table_ranges", {}).get(rel, []):
            if start <= line <= end:
                return name
        return None

    def dispatch_tests(self, table: str):
        """Tests that run any line reading the table, or a table derived from it.
        None when no reading site was ever executed (caller falls back to the broad rule)."""
        readers = self.index.get("table_readers", {})
        uses = self.index.get("table_uses", {})
        seen, todo = set(), [table]
        while todo:
            t = todo.pop()
            if t not in seen:
                seen.add(t)
                todo.extend(readers.get(t, []))
        out: set[int] = set()
        for t in seen:
            for rel, stmt, ln in uses.get(t, []):
                if rel.startswith("tests/"):
                    out |= self.by_file.get(rel, set())
                else:
                    out |= self.cov(rel, {stmt, ln})
        return out or None
```

In `sqlglot_change`, compute once before the loop:

```python
        kinds = table_edit_kinds(old_tree, new_tree) if rel.startswith(("sqlglot/parser.py", "sqlglot/parsers/")) else {}
```

Then in the `else:` branch (no enclosing function), replace the `lam = ...` block with:

```python
                tbl = self.table_at(rel, ln)
                kind = next((k for (c, n), k in kinds.items() if n == tbl), None) if tbl else None
                lam = self.lambda_range(rel, ln)
                if kind == "lambda":
                    # Only lambda bodies changed in this table. A neighbour line of an insertion is not a change.
                    if lam:
                        out |= self.cov(rel, range(lam[0], lam[1] + 1))
                        reasons.append("lambda")
                    continue
                if kind == "structural":
                    d = self.dispatch_tests(tbl)
                    if d is not None:
                        if lam:
                            d |= self.cov(rel, range(lam[0], lam[1] + 1))
                        scope = None
                        if rel.startswith(DIALECT_DIRS) and Path(rel).stem not in ("__init__", "base"):
                            scope = self.import_time_change(rel, ln)  # today's dialect scope
                        out |= d if not scope else d & scope
                        reasons.append("table")
                        continue
                # otherwise: existing lambda handling, then the existing module-level path
                hit = self.cov(rel, range(lam[0], lam[1] + 1)) if lam else set()
                if hit:
                    out |= hit
                    reasons.append("lambda")
                    continue
```

(keep the existing `__init__.py` and `import_time_change` code below as the fallback.)

Why this is recall-safe:
- It is a subset of today's selection (dialect files) or a replacement of "everything" (`parser.py`, `base.py`) by "every test that executes a line that reads the table, or a table derived from it".
- A test that can observe an entry change must read the table or its derived table. The index scan is by attribute name, so it covers `self.X`, `parser.Parser.X` and local aliases (`column_operators = self.COLUMN_OPERATORS` at `parser.py:7401` is still an `Attribute`).
- No dynamic access exists (F1). Test source readers are added as whole files (F5/R4).
- Any miss in the index or an unexecuted site returns `None`, so the old broad path runs.
- `kind` is exact because the statement-level dump is compared with lambda bodies stripped. Adding, removing or renaming a key, changing a value or a builder argument, or changing a lambda's arguments is "structural". Only body-only lambda edits are "lambda".
- This also fixes R1 and R2: an insertion changes the dump, so it becomes "structural" and goes to the table-level dispatch.

**What it does for each edit shape** (kind → selection):
- Lambda body edit, key unchanged → lambda coverage (unchanged).
- Entry added or removed → `dispatch(T)` ∪ old lambda coverage (R1 fixed).
- Key renamed on a lambda line → `dispatch(T)`.
- Builder argument or reference edit (`_show_parser(...)`, `build_x`, `exp.Y`) → `dispatch(T)`. For `gbav`-shaped edits this is 36-40 tests (SHOW_PARSERS), intersected with the singlestore family.
- Token set / derived set edit → `dispatch(closure(T))`, for example `ID_VAR_TOKENS` closing over `TABLE_ALIAS_TOKENS`, `COMMENT_TABLE_ALIAS_TOKENS`, `UPDATE_ALIAS_TOKENS`, `WINDOW_ALIAS_TOKENS` and `FETCH_TOKENS`.

### c3. Optional (not recommended yet): key-text narrowing

For string-keyed tables, intersect `dispatch(T)` with tests whose strings contain the key, plus tests that load fixtures.
- Why it is unsafe: SQL reaches the parser from fixture files, from `sqlglot/` source strings (`maybe_parse("...")`, rewrite helpers), and from f-strings in tests. `test_strings` (`maps/impact.json.gz`) only holds test-file literals of 3-200 chars.
- If you do it, it must also keep every test that covers `tests/helpers.py` fixture loaders and every test that calls `parse` inside sqlglot source helpers. The gain is limited to `FUNCTIONS` (about 746 tests to a few dozen, maybe 10-20 s per such commit). I would not take that risk for one miss at 300 s.

## (d) Ranked recommendations

1. **Fix R1/R2 (recall).** Structural diff of class-level table statements (`table_edit_kinds`) and route non-lambda-body changes away from the lambda rule. Impact: removes a real miss shape (new or renamed table keys next to lambdas, which are common in "add function X" commits). Cost: none, because dispatch is still cheaper than `import_time_change` for most tables. This is the one change with a recall payoff.
2. **Dispatch coverage for `parser.py` and `parsers/base.py` tables** (`dispatch_tests` plus `table_uses` and `table_readers` in the index). For small and medium tables (SHOW/SET/ALTER/PROPERTY/CONSTRAINT/ANALYZE/option tables, `FUNCTIONS`, `FUNCTION_PARSERS`) it brings the commit from about 36.8 s to about 2-27 s. Expected saving: about 10-30 s on each commit touching such an entry. In the sample only 1 of 30 commits was a table entry, so expect about 1-3 commits in a 45-commit final set, roughly 20-60 s total.
3. **Intersect with the dialect scope for dialect-file tables.** The code in c2 does this; it makes the new rule a strict subset of today's selection there. Impact: removes `file_cov(family)` bulk for edits like `gbav` (36-40 tests instead of the family union), with no new recall risk beyond dispatch exactness.
4. **Index test-source readers** (`F5`/`R4`). Small but cheap: whole-file selection of `test_generator`, `test_dialect` and `test_snowflake` when `FUNCTIONS`, `CREATABLES` or `DESCRIBE_QUALIFIER_PARSERS` change structurally.
5. **Hot tables stay broad on purpose** (`TYPE_TOKENS`, `ID_VAR_TOKENS`, `STATEMENT_PARSERS`, `RANGE_PARSERS`, `COLUMN_OPERATORS`, `NO_PAREN_FUNCTIONS`, etc.). Their dispatch coverage is 1200+ tests, so nothing is recoverable; do not add key-text narrowing (c3).
6. **Guard against import-time breakage.** If a structural table edit references a name that the new file does not define (a typo, a missing `exp.X`), the import fails and every test fails. A cheap static guard would compare the new table's referenced names to the module namespace, and select all on failure. It is low priority because upstream commits do not break import.

Cross-links: `exp.FUNCTION_BY_NAME` feeds `FUNCTIONS` (`parser.py:402`), so the expression-class research should treat "a new `Func` subclass" as a `FUNCTIONS` structural change and select `dispatch_tests("FUNCTIONS")`.
