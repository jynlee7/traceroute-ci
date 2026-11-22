# 04 — Dialect registry, lazy loading, and the dialect family

Scope: `sqlglot/dialects/__init__.py`, `sqlglot/dialects/dialect.py` (the `Dialects` enum, the `_Dialect`
metaclass and what it precomputes when a class is created), how `dialects/X.py` ties to
`parsers/X.py`, `generators/X.py` and `typing/X.py`, the real inheritance graph, and whether
`selector.py:dialect_family` (lines 262-283) captures it. Paths are relative to `work/sqlglot/` unless they start
with `selector.py` or `tools/`.

## (a) Findings

**F1. Registry and lazy loading.**
- `dialects/__init__.py:67-101` lists 33 class names in `DIALECTS`.
- `:103` sets `MODULE_BY_DIALECT = {name: name.lower()}`, so module stem == lower-cased class name is a hard invariant.
- `__getattr__` (`:120`) imports `sqlglot.dialects.<stem>` on first attribute access.
- `_Dialect._try_load` (`dialect.py:238-256`) imports `sqlglot.dialects.{key}` and registers the class only if `attr.__name__.lower() == key`.
- `_Dialect.classes` (`dialect.py:230-236`) imports **every** dialect module when it runs.
- `_Dialect.__new__` (`dialect.py:306-309`) registers each class under `Dialects[clsname.upper()].value`, or under `clsname.lower()` when the enum has no such member.

**F2. Enum value vs file stem: no mismatches.** Every `Dialects` value (`dialect.py:98-133`) equals its file stem:
`tsql`, `spark2`, `databricks`, `risingwave`, `clickhouse`, `bigquery`, `duckdb`, `starrocks`, `prql`, `dax`, and so on.
Three registry entries are not plain enum members:
- `SingleStore` has a module (`dialects/singlestore.py`) and is in `DIALECTS`, but it is **missing from the enum**. It still registers as `"singlestore"` through the `clsname.lower()` fallback (`:309`), which equals its stem.
- `Dialects.DIALECT = ""` is the base `Dialect`.
- `executor/python.py:553` defines `class Python(Dialect)`. It registers as `"python"` and lives outside `DIALECT_DIRS`; its generator is `generators/python.py`.
- Test files: every dialect has `tests/dialects/test_<stem>.py` except `spark2`, which has none. `test_pipe_syntax.py` uses the base dialect.
- Net: the selector's "word == stem" assumption holds. **No mismatch to fix.** The SingleStore gap matters only for tests that iterate the enum (F6).

**F3. How the X files tie together.** It is all explicit imports plus class attributes:
- `dialects/X.py` imports `parsers.X.XParser` and `generators.X.XGenerator` and assigns `Parser = XParser` and `Generator = XGenerator`. Example: `dialects/postgres.py:5-6,165,167`.
- When typing exists, it also imports `typing.X.EXPRESSION_METADATA` (`dialects/hive.py:16,37`).
- Tokenizers are nested classes inside `dialects/X.py`, e.g. `dialects/tsql.py:160`. **There is no `sqlglot/tokenizers/` directory**, so that entry in `selector.py:24` is dead.
- `__new__` resolves `tokenizer_class`, `parser_class` and `generator_class` from `klass.__dict__` or the first base (`dialect.py:331-348`). Dialects without their own typing file (trino, athena, dune, doris, starrocks, singlestore, fabric, materialize, risingwave, ...) inherit `EXPRESSION_METADATA` from the parent's class body.

**F4. Class-creation precomputation (runs at import, so coverage cannot attribute it).** `dialect.py:311-432` builds the following from class attributes:
- the time and format tries and their inverses (`:311-328`);
- `INVERSE_CREATABLE_KIND_MAPPING`;
- the quote, identifier, bit, hex, byte and unicode delimiters from `tokenizer_class`;
- the escape tables;
- `SUPPORTS_COLUMN_JOIN_MARKS`;
- `INITCAP_SUPPORTS_CUSTOM_DELIMITERS` (`:424`);
- `VALID_INTERVAL_UNITS`.

It also **mutates the generator's `TRANSFORMS` in place** (`:350-355`, `gen_cls.TRANSFORMS.pop(part)`). Four generators define no `TRANSFORMS` of their own: `generators/dax.py`, `dune.py`, `prql.py` and `solr.py`. For them the pop hits the parent's dict (base `Generator` or `TrinoGenerator`), so an edit to the **child's** `SUPPORTED_JSON_PATH_PARTS` would change the **parent** dialect. That is an upward edge the family walk never follows. Today it is idempotent, because the parts are inherited unchanged.

**F5. The real inheritance graph** (dialect classes, mirrored in parsers and generators). All edges come from the `class`/`from` lines quoted:
```
Dialect ─┬─ Presto (dialects/presto.py:19) ── Trino (trino.py:9) ─┬─ Athena (athena.py:14)
         │                                                        └─ Dune (dune.py:9)
         ├─ Hive (hive.py:19) ── Spark2 (spark2.py:12) ── Spark (spark.py:14) ── Databricks (databricks.py:15)
         ├─ MySQL (mysql.py:16) ─┬─ Doris (doris.py:10) ├─ StarRocks (starrocks.py:11) └─ SingleStore (singlestore.py:8)
         ├─ Postgres (postgres.py:11) ─┬─ Materialize ├─ Redshift └─ RisingWave
         ├─ TSQL (tsql.py:15) ── Fabric (fabric.py:12)
         ├─ Python (executor/python.py:553)     [outside DIALECT_DIRS]
         └─ BigQuery, ClickHouse, DAX, Dremio, Drill, Druid, DuckDB, Exasol, Oracle, PRQL, SQLite,
            Snowflake, Solr, Tableau, Teradata  (direct)
```
Parsers: `AthenaParser(AthenaTrinoParser(TrinoParser))` (`parsers/athena.py`). Typing: `typing/databricks → spark → spark2 → hive` and `typing/redshift → postgres`.

Cross-dialect **helper** edges (not inheritance):
- `dialects/presto.py:11,22,36`: Presto uses `MySQL.TIME_FORMAT` and `MySQL.TIME_MAPPING`.
- `generators/presto.py:31,623`: `Hive.INVERSE_TIME_MAPPING`.
- `generators/athena.py`: `_HiveGenerator(HiveGenerator)`.
- `parsers/athena.py:12-13,42-43`: lazy `Hive` and `Trino`.
- `generators/drill.py`: `generators.mysql.date_add_sql`.
- `parsers/singlestore.py:10`: `MySQL.format_time`.
- Outside DIALECT_DIRS: `generator.py:5744` (lazy `sqlglot.dialects.tsql`, `TSQL.CONVERT_FORMAT_MAPPING`) and `optimizer/pushdown_predicates.py:36-37` (lazy `Athena` and `Presto`).

String-literal (import-free) edges:
- `parsers/spark2.py:155`: `dialect="spark"`. Here Spark2 depends on its **child**.
- `parsers/mysql.py:39,134`: `Dialect["mysql"]`.

**F6. What `dialect_family` computes.** It follows same-stem files plus `rdeps` restricted to `DIALECT_DIRS`, transitively (`selector.py:262-283`). From the `build_index.py` import edges, the families are:
- hive → {hive, spark2, spark, databricks, presto, trino, athena, dune} (presto joins through `generators/presto.py`, which imports `dialects/hive`)
- mysql → {mysql, doris, starrocks, singlestore, drill, presto, trino, athena, dune}
- postgres → {postgres, materialize, redshift, risingwave}
- presto → {presto, trino, athena, dune}
- trino → {trino, athena, dune}
- tsql → {tsql, fabric}
- spark2 → {spark2, spark, databricks}
- leaf dialects → themselves

**Conclusion: it captures every real subclass, and it correctly adds the helper consumers.** Subclass dialects are found correctly. The gaps are elsewhere (F7-F9).

**F7. Coverage of dialect modules' import-time lines is attributed to whichever test imported them first, not to consumers.**
- `maps/impact.json.gz`: line 1 of `sqlglot/dialects/hive.py` maps to just 4 `test_optimizer` tests (`test_annotate_funcs`, `test_bracket_annotation`, ...).
- `risingwave.py:1` maps to 6 unrelated tests, including `test_druid`.
- `dialects/dialect.py`, `parsers/base.py`, `generators/python.py` and `dialects/__init__.py` have **empty** sets on their import lines.

So for class-body edits, `file_cov` over the family is only a proxy. The real recall comes from `name_tests` (stems) and `class_tests` (refs inside `sqlglot/`).

**F8. Six tests iterate the whole registry and contain no dialect word.** Checked against `test_strings` in the map:
- `tests/test_time.py:26` (`test_canonical_time_format_vocabulary`): reads every `TIME_MAPPING` and `FORMAT_MAPPING`.
- `tests/test_generator.py:20` (`test_generator_memory`, 6.7 s).
- `tests/dialects/test_spark.py:1659` (`test_strip_modifiers`).
- `tests/dialects/test_dialect.py:225-231` (`test_enum`, uses `Dialect.classes`).
- `tests/dialects/test_dialect.py:3875` (`test_partition_by_excluding`).
- `tests/dialects/test_druid.py:20` (`test_druid`).

None of them holds a dialect name as a string. `test_canonical_time_format_vocabulary` (map idx 1568) appears in the family `file_cov` of **no** dialect stem. The others appear only by accident of import order, e.g. not at all for spark, trino, dremio or drill.

**F9. Optimizer fixtures name dialects only inside `.sql` files.** For example:
- `tests/fixtures/optimizer/annotate_functions.sql` has 1888 `dialect:` lines (snowflake 852, bigquery 420, mysql 175, hive 106, ...).
- `qualify_columns.sql` and `canonicalize.sql` also carry `dialect:` lines.

These are read via `check_file("<name>")` and `load_sql_fixture_pairs("optimizer/<name>.sql")` (`tests/test_optimizer.py:226-232`). In the map, the strings of `test_annotate_funcs` hold only `duckdb`, and those of `test_qualify_columns` lack `hive` and `mysql`. The words `dialect="..."` and `write={...}` never appear there.

**F10. Strings like `"duckdb, version=1.0"` and `"mysql, normalization_strategy = ..."` are never whole-string equal to a stem.** Counted in tests: duckdb 13, spark 6, clickhouse 6, postgres 4, and others. `name_tests` (`selector.py:250-260`) misses them, unless the test file is `test_<stem>.py`.

## (b) Recall risks and cost waste

**Recall risks** (patch shapes that miss today):
1. **A class-body edit to `TIME_MAPPING`/`FORMAT_MAPPING` in any enum dialect** (e.g. `dialects/presto.py:36`, `dialects/hive.py`): this breaks `test_canonical_time_format_vocabulary`, which is never selected (F8). The same holds for a tokenizer `KEYWORDS` or `QUOTES` edit that breaks `test_partition_by_excluding`, `test_druid` or `test_strip_modifiers` for, say, trino, spark or dremio.
2. **A class-body edit that breaks `_Dialect.__new__` or import** (e.g. a bad tokenizer `_QUOTES` or `FORMAT_MAPPING` entry, `dialect.py:358-372`): all six F8 tests fail, including the 6.7 s `test_generator_memory`. Only the stem-named tests are selected.
3. **An edit to the `Dialects` enum or the `DIALECTS` list**: this is a module-level change in `dialect.py`. The family is "everything", but the F8 tests are still not added, because they hold no stems.
4. **A non-lambda class-body or table edit in `dialects/mysql.py`, `hive.py` or `tsql.py`** (`COERCES_TO`, `TYPE_*`, `EXPRESSION_METADATA` overrides) that changes `annotate_functions.sql` results: the only guard for `test_annotate_funcs` is the incidental `file_cov` of `parsers/X.py`. For small dialects (risingwave, athena, trino) that is thin (F9).
5. **A module-level edit in `generators/python.py`** (e.g. a `TRANSFORMS` entry that is not a lambda): the family is `{generators/python.py}` only, because `executor/python.py` is filtered out by `DIALECT_DIRS`. The word `python` matches almost nothing, and the import lines have empty sets. Recall rests on function-line `file_cov` (53 tests).
6. **A class-body edit to `dialects/spark.py`** (tokenizer or normalization): Spark2's parser builds pivot names with `dialect="spark"` (`parsers/spark2.py:155`). Tests holding only `"spark2"` or `"hive"` are not in the spark family.
7. **A change to `SUPPORTED_JSON_PATH_PARTS` in `generators/{dax,dune,prql,solr}.py`**: it mutates the parent's `TRANSFORMS` (F4). Base or Trino tests can break while the family only goes downward. Low probability.

**Cost waste:**
- The helper edges inflate families: hive pulls in presto, trino, athena and dune; mysql pulls in presto, trino, athena, dune and drill. They are genuine consumers (`Hive.INVERSE_TIME_MAPPING`, `MySQL.TIME_MAPPING` are read at Presto **class-creation time**, `dialects/presto.py:36`), and those reads are import-time lines with no useful coverage. **Do not narrow** them to ref-line coverage, because that would lose recall.
- `dialects/dialect.py` is treated as a "dialect" (its stem `dialect` is not excluded), so any class-body edit in it selects every family. That is correct: Dialect class attributes are inherited by all of them.

## (c) Proposed code changes

**C1. `tools/build_index.py`: add dialect metadata and test facts.** Add `import re` and the function below. Call `index.update(dialect_meta(root, trees, toplevel, subs, rdeps))` inside `build()` before the `return`, after the `rdeps` loop. `rdeps` is mutated in place, so do this before `rdeps` is converted to sorted lists.
```python
def dialect_meta(root, trees, toplevel, subs, rdeps) -> dict:
    # Files defining a Dialect subclass anywhere (e.g. sqlglot/executor/python.py).
    names, todo = set(), ["Dialect"]
    while todo:
        for s in subs.get(todo.pop(), ()):
            if s not in names:
                names.add(s); todo.append(s)
    dialect_files = sorted({rel for rel, rows in toplevel.items() for _, _, n in rows if n in names})
    # Stems iterated by `for d in Dialects` (the enum), vs. every registry module.
    enum_stems, module_stems = set(), set()
    for node in ast.walk(trees.get("sqlglot/dialects/dialect.py", ast.Module(body=[]))):
        if isinstance(node, ast.ClassDef) and node.name == "Dialects":
            for a in node.body:
                if isinstance(a, ast.Assign) and isinstance(a.value, ast.Constant) and a.value.value:
                    enum_stems.add(a.value.value)
    module_stems = {p.stem for p in (root / "sqlglot/dialects").glob("*.py")} - {"__init__", "dialect"}
    known = enum_stems | module_stems
    # String-literal dialect use inside sqlglot: dialect="x", Dialect["x"], get_or_raise("x").
    for rel, tree in trees.items():
        for n in ast.walk(tree):
            lits = []
            if isinstance(n, ast.Call):
                lits += [k.value for k in n.keywords if k.arg in ("dialect", "read", "write")]
                if getattr(n.func, "attr", "") == "get_or_raise":
                    lits += n.args[:1]
            elif isinstance(n, ast.Subscript) and getattr(n.value, "id", "") == "Dialect":
                lits.append(n.slice)
            for c in lits:
                if isinstance(c, ast.Constant) and c.value in known:
                    target = f"sqlglot/dialects/{c.value}.py"
                    if target != rel:
                        rdeps.setdefault(target, set()).add(rel)
    # Tests: registry iteration, fixture dialects, and "x, k=v" heads.
    enum_tests, registry_tests, words = [], [], {}
    fx = re.compile(r"dialect:\s*([a-z0-9]+)")
    for p in sorted((root / "tests").rglob("test_*.py")):
        rel = str(p.relative_to(root))
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        def scan(fn, tid):
            w = set()
            for n in ast.walk(fn):
                it = getattr(n, "iter", None)
                if isinstance(it, ast.Name) and it.id == "Dialects":
                    enum_tests.append(tid)
                if isinstance(n, ast.Attribute) and n.attr == "classes" and getattr(n.value, "id", "") == "Dialect":
                    registry_tests.append(tid)
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and len(n.value) < 120:
                    s = n.value
                    if "," in s and "=" in s and s.split(",")[0].strip() in known:
                        w.add(s.split(",")[0].strip())
                    for cand in (root / "tests/fixtures" / s, root / "tests/fixtures/optimizer" / f"{s}.sql"):
                        if cand.is_file() and cand.suffix == ".sql":
                            w |= set(fx.findall(cand.read_text(errors="ignore"))) & known
            if w:
                words[tid] = sorted(w)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test"):
                scan(node, f"{rel}::{node.name}")
            elif isinstance(node, ast.ClassDef):
                for it in node.body:
                    if isinstance(it, ast.FunctionDef) and it.name.startswith("test"):
                        scan(it, f"{rel}::{node.name}::{it.name}")
    return {"dialect_files": dialect_files, "enum_stems": sorted(enum_stems),
            "module_stems": sorted(module_stems), "enum_tests": sorted(set(enum_tests)),
            "registry_tests": sorted(set(registry_tests)), "test_words": words}
```
`fixtures/optimizer/x.sql` paths are tried only for string constants, which is cheap, and all of this runs at build time, which is not charged.

**C2. `selector.py` `__init__`: merge `test_words` into `string_sets`.** Insert right after `self.index = ...` (after line 206):
```python
        for tid, ws in self.index.get("test_words", {}).items():
            n = self.by_id.get(tid)
            if n is not None:
                self.string_sets[n] = self.string_sets.get(n, frozenset()) | frozenset(ws)
```

**C3. `selector.py` `dialect_family`: follow Dialect-defining files outside `DIALECT_DIRS`.** This catches `executor/python.py`.
```diff
-        rdeps = self.index.get("rdeps", {})
+        rdeps = self.index.get("rdeps", {})
+        extra = set(self.index.get("dialect_files", ()))
@@
-            todo.extend(r for r in rdeps.get(cur, []) if r.startswith(DIALECT_DIRS) and Path(r).stem not in ("__init__", "base"))
+            todo.extend(r for r in rdeps.get(cur, [])
+                        if (r.startswith(DIALECT_DIRS) or r in extra) and Path(r).stem not in ("__init__", "base"))
```
Also make the same-stem step include `executor/python.py` when `cur` is `generators/python.py`. Simplest: add `"sqlglot/executor/"` handling via `extra` stems:
```python
            todo.extend(f for f in extra if Path(f).stem == stem)
```
C1's string edges arrive through `rdeps`, so `parsers/spark2.py` now joins the spark family (risk 6).

**C4. `selector.py` `import_time_change`: add registry-iterating tests.** Insert before `cls = self.top_class(...)`:
```python
        stems = {Path(f).stem for f in family}
        reg = []
        if stems & set(self.index.get("enum_stems", ())) or "dialect" in stems:
            reg += self.index.get("enum_tests", [])
        if stems & set(self.index.get("module_stems", ())) or "dialect" in stems:
            reg += self.index.get("registry_tests", [])
        gen_mem = "tests/test_generator.py::TestGenerator::test_generator_memory"
        for tid in reg:
            if tid == gen_mem and not (rel.startswith("sqlglot/generators/") or rel.endswith("dialects/dialect.py")):
                continue  # 6.7 s: only when generator construction itself may change
            n = self.by_id.get(tid)
            if n is not None:
                hit.add(n)
```
`enum_tests` excludes SingleStore and Python, which matches the enum, while `registry_tests` (`test_enum`) covers singlestore. Optionally, gate `gen_mem` back in for any dialect-module edit that adds or removes top-level names (an import-break shape) if a miss audit shows it is needed.

**C5. Housekeeping.** `"sqlglot/tokenizers/"` in `DIALECT_DIRS` (`selector.py:24`) is dead: no such directory exists. Keep it, since it is harmless, but fix the comment on line 23.

## (d) Ranked recommendations

1. **C4 + C1 (enum_tests/registry_tests).** Closes recall risks 1-3. It adds 5 tests of about 0.1-0.5 s each to every dialect class-body edit, with `test_generator_memory` gated. Expected impact: removes the most likely remaining dialect-level miss, at very low cost.
2. **C1 fixture words + C2.** `test_annotate_funcs`, `test_qualify_columns`, `test_canonicalize` and similar get the dialects their fixtures use (risk 4). These are optimizer tests, so they cost a few seconds at most. Medium recall gain for snowflake, bigquery, mysql and hive class-table edits.
3. **C3 (`dialect_files`).** Ties the executor's `Python` dialect to `generators/python.py` (risk 5). Small cost, low-to-medium recall gain.
4. **C1 string-literal rdeps.** Fixes Spark2→Spark (risk 6) and similar edges. Negligible cost.
5. **C1 `"x, k=v"` heads.** Mostly redundant with `test_<stem>.py`. Cheap insurance.
6. **Do not narrow** helper-import families (hive→presto/trino/athena/dune, mysql→presto/drill). Those reads happen during class creation (F4), where coverage is blind.
7. **Optional:** for edits to `SUPPORTED_JSON_PATH_PARTS` in generators without their own `TRANSFORMS` (dax, dune, prql, solr), also add the parent's family (risk 7). Low value.
8. **No fix needed** for enum-value/stem mismatches (F2). Recording `SingleStore`'s absence from `Dialects` in `enum_stems` (C1) is enough.
