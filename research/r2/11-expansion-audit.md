# R2-11: audit of the widening/expansion rules in selector.py

Scope: every rule that widens a selection past the changed line's own coverage. These are mostly the round-1 additions inside `_import_time_change` (selector.py:386-413), `in_import_fn` (:331, fed by `build_index.py` `import_fns`), the lambda paths (:535-546), `new_method_tests` (:574-593) and `name_tests` (:262-279).

Method: I re-derived each rule's component sets with read-only Python over `maps/impact.json.gz`, `work/index.json` and `sample/tests.json`. I re-implemented `cov`, `file_cov`, `dialect_family`, `name_tests`, `class_tests`, `sql_name_tests` and LPT costing on the data and did not import selector.py or sqlglot. Check: my reconstructed unions match the selection sizes in `work/why.log` exactly (4d63 561, 535z 171, gbav 42, ktz3 134, fryt 37). Costs below are LPT `max load + 2 s·k` with the best k.

## (a) Findings

### Which sample commits reach an expansion rule at all
Only 6 of the 30 commits do: `module` on 4d63, 535z, gbav and ktz3, `import-fn` on fryt, and `lambda` on 6ze8. All the others go through `fn`, fixtures or AST-inert. Of those 6, **only 535z (1434) and 6ze8 (302) fail anything**. Both failures are already in the changed line's own coverage: 535z line-cov is 7 tests and contains 1434, and 6ze8's lambda body coverage contains 302. **No widening rule was needed for any sample failure.** That does not prove the rules are useless, because they guard hidden shapes. It does mean that every second they add on the sample is pure cost.

### Per-rule decomposition (unique = tests no other part of the union adds)
| commit | part | n | Σs | unique n / Σs | biggest unique test | cost without the part |
|---|---|---|---|---|---|---|
| 4d63 snowflake Tokenizer attr (27.5 s) | fixture-header dialects (`name_tests` :275-278) | 231 | 51.5 | **201 / 46.2** | test_execute_tpcds 21.5, execute_tpch 3.9, optimized_tpch 3.5 | **12.7** |
| | all_dialect_tests | 6 | 6.8 | 3 / 6.7 | test_generator_memory | 27.5 (tpcds dominates) |
| | family file_cov (4 files) | 346 | 11.0 | 1 / 0.0 | – | 27.5 |
| | dialect-name strings | 303 | 7.4 | 10 / 0.0 | – | 27.5 |
| | test file, class_tests, heads | ≤116 | ≤2.2 | 0 | – | 27.5 |
| 535z tsql `WEEK_OFFSET` (11.2 s) | all_dialect_tests | 6 | 6.8 | 2 / 6.7 | test_generator_memory | **7.9** |
| | family file_cov (tsql+fabric, 7 files) | 166 | 7.8 | 0 | holds the failure 1434 | 11.2 |
| | strings / heads / fixture-hdr / class | – | – | ≤2 / 0.0 | – | 11.2 |
| gbav singlestore SHOW_PARSERS row (10.1 s) | all_dialect_tests | 6 | 6.8 | 5 / 6.7 | test_generator_memory | **3.4** |
| | dialect-name strings | 24 | 1.3 | 4 / 0.8 | – | 9.3 |
| fryt dremio `_builder` body (9.2 s) | all_dialect_tests (reached via import-fn) | 6 | 6.8 | 6 / 6.8 | test_generator_memory | **2.4** |
| ktz3 `TriggerProperties.arg_types` (7.1 s) | file_cov(expressions/ddl.py) | 128 | 5.9 | 127 / 5.9 | – | **2.4** (class_tests ∪ sql_name only, 7 tests) |

### F1. The fixture-header rule is a substring bug (selector.py:338-344, used at :275-278)
`fixture_tests` matches `stem in s` for any test string. For `optimizer.sql`, the stem `optimizer` is a substring of `optimizer/tpc-ds/tpc-ds.sql` and `/optimizer/tpc-h/`, which are strings of test_execute_tpcds and the TPC tests. So 231 tests (51.5 s) come in, when the right reader is `test_optimize` (`check_file("optimizer", ...)`). The same thing happens with `qualify_columns.sql` (167 tests, 19.8 s, through the helper string `validate_qualify_columns` and `optimizer/`) and `canonicalize.sql` (167 tests). Exact matching gives 1 test each:

| fixture | substring | exact (`s == stem`, or `s` ends with `optimizer/<stem>.sql` / `/<stem>`) |
|---|---|---|
| optimizer.sql | 231 / 51.5 s | 3 / 0.5 s (test_optimize + 2 sqlglotc tests) |
| qualify_columns.sql | 167 / 19.8 s | 1 / 1.4 s |
| canonicalize.sql | 167 / 19.8 s | 1 / 1.0 s |
| the other 12 header fixtures | 1 each | the same 1 each |

The dialects that pull in TPC-DS through this bug are bigquery, duckdb, mysql, postgres, snowflake, spark and spark2, plus the `normalization_strategy = lowercase` header. Clickhouse, oracle, presto, redshift, risingwave, sqlite and starrocks pull in test_scope and test_tpcds (19.8 s). This is the single most expensive widening, and it rests on a matching error rather than on a real dependency.

### F2. `test_generator_memory` in `all_dialect_tests` is redundant (selector.py:405-412, build_index.py all_dialect_tests)
The test (tests/test_generator.py:19-38) does `Dialect.get_or_raise(name)` for every `Dialects` member, then calls `dialect.generator()` 1,100 times and asserts the memory growth is under 1 KB. An import-time or class-body edit can make it fail in only two ways:
- **The dialect module fails to import, or `Generator()` raises.** That is also caught by `test_dialect::test_enum` (:224-231, which does `Dialect[d]` for every enum member, so every module is imported) and `test_partition_by_excluding` (:3875, which parses and generates in every dialect except DAX and PRQL). Both are in `all_dialect_tests` and cost about 0.05 s. It is also caught by the dialect's own `test_<d>.py`. Every enum member has one (tests/dialects/ lists them all; spark2 is covered by test_spark strings).
- **A per-instance leak.** That needs code that runs on each `Generator()` call (`generator.py:950-1010` `__init__`), and an edit there goes through the `fn` rule, whose coverage already credits generator_memory. Class-body attributes are evaluated once at import, so they cannot leak per instance.

Round-1 (research/11-slow-tests.md R1, research/04-dialect-registry.md:113) added it for the "import breaks in a non-core dialect" shape. test_enum covers that shape for about 1/100th of the cost.

### F3. The `_build` prefix seed marks about 120 parse-time functions as import-time (build_index.py import_fns, `once = once or name.startswith("_build")`)
I replayed the seeding on the pinned AST with and without the prefix clause. The clause alone adds `_builder` in 9 files and, for example, 16 `_build_*` functions in parsers/snowflake.py and 13 in parsers/bigquery.py, plus their same-file callees.

In sqlglot, `_builder` is the closure that a factory returns, and `_build_x` is usually placed into `FUNCTIONS` by reference (`"DATE": _build_date`). Both run **per parse** and are recorded per test by coverage. The functions that really run at import are already seeded by `scan()`, because they are called at module or class level (`_build_gen_dispatch`, `_build_coerces_to`, `build_date_delta_with_cast_interval(...)`). The one real cache-gated case is `generator.py:_build_dispatch`, stored into `_DISPATCH_CACHE` at :1005-1008.

The effect is that an edit inside one of these builders goes to `import-fn`, which applies family file_cov, name tests, all_dialect_tests and, through F1, TPC-DS. fryt cost 9.2 s where the `fn` window would give 7 tests and 2.2 s while still containing the answer. For a bigquery, snowflake, postgres or mysql builder, today's cost is about 27.5 s. Parse-time builders are a very common mutation target: one of the 30 sample commits is exactly this.

### F4. file_cov of the whole `expressions/*.py` file on a class-body edit (selector.py:388-391)
`dialect_family` returns `{rel}` for non-dialect files, so a class-body edit takes coverage of every line in the module. In the expressions package the classes in one file only interact through inheritance, and `class_tests` already follows inheritance through `subs`, lines that name the class, and `<lower>_sql`. `sql_name_tests` adds tests that call the class by its SQL name. Here is the median cost of a class-body edit per file, computed over every top-level class:

| file | classes | now | class_tests ∪ sql_name only |
|---|---|---|---|
| functions.py | 96 | 25.7 s | 4.8 s |
| query.py | 146 | 27.5 s | 4.5 s |
| ddl.py | 56 | 7.4 s | 4.0 s |
| datatypes.py / core.py | 7 / 117 | 27.5 / 36.8 s | 25.5 / 25.5 s |
| others | – | 4-9 s | almost the same (file_cov is tiny) |

An edit that breaks import (a bad `arg_types` literal, for example) fails every test, so any non-empty selection catches it. The `module-level-unmapped` fallback (:552) still covers the empty case.

### F5. `new_method_tests` (selector.py:574-593) widens on generic and nested names
`def_names` (:741-758) collects defs **at any depth**. A new local helper named `_builder`, `func` or `_parse` therefore selects the coverage of every same-named definition: `_builder` has 25 definitions (9.7 s), `func` 7 (27.5 s), `_parse` 6 (all tests, 36.8 s). Round-1 review C6 noted this but it was not fixed. A local closure cannot override anything or be found by name.

Even for real methods, the rule takes **every** definition of the name across sqlglot. Adding an override of `_parse_types` or `_parse_column` in one dialect parser therefore selects every test (36.8 s), although only instances of that class and its subclasses can see it. Over the 2,435 names in `fns`, the median cost is 5.1 s, p75 is 25.5 s, and 760 names cost ≥ 20 s. The rule never fired on the sample. On hidden commits it fires only when a patch adds a def name that already exists. A rename to a fresh name is free here, and the old name is handled by the `fn` window on the header line.

### F6. Rules that are cheap or are the real signal: keep them as they are
- **Dialect family file_cov, including subclass dialects through `rdeps`.** It holds 535z's failing test and is how attribute reads by base code get mapped to "tests that use dialect X". It adds 2-5 s over line-cov. Keep it, because it is the core protection for class attributes (535z is exactly that shape).
- **Dialect-name strings, the `test_<d>.py` file, and `"x, k=v"` heads.** At most 10 unique tests and 0.8 s on the sample.
- **`class_tests` and `<lower>_sql`.** No unique cost on the dialect commits, and it is the *only* signal for ktz3, where line-cov is empty.
- **`sql_name_tests`.** It added 0 tests on the sample. It falls back to run-everything on `*` or more than 200 subclasses, which is correct for base classes.
- **lambda and lambda-adjacent.** 6ze8 takes 5 tests and they contain the failure. The adjacent case adds only the lambda's coverage on top of the module rule.
- **package `__init__` → all** and **lost internal name → all.** Rare and correct (the whole import fails).
- **Base-file class tables** (an edit to a row of `parser.py`, `tokens.py` or `generator.py` gives `file_cov` ≈ all tests, 36.8 s). This is the largest *hidden-set* cost driver among the module rules, but narrowing it is round-1 agent 01's parser-table work, so it is not repeated here.

## (b) Savings and recall risk
Sample (217.7 s now):

| change | 4d63 | 535z | gbav | fryt | ktz3 | total |
|---|---|---|---|---|---|---|
| P1 exact fixture match | 27.5→12.7 | – | – | – | – | **14.8** |
| P2 drop slow tests from all_dialect_tests | 12.7→9.5 | 11.2→7.9 | 10.1→3.4 | 9.2→2.5 | – | **19.8** |
| P3 `_build` prefix → cache-store seed | – | – | – | 2.5→2.2 | – | 0.3 (about 7 s without P2, about 25 s for big dialects without P1) |
| P4 no expressions file_cov when the class signal is non-empty | – | – | – | – | 7.1→2.4 | **4.7** |
| P5 new-def narrowing | – | – | – | – | – | 0 (never fires) |

**P1+P2+P3+P4 ≈ 39.6 s on the sample (217.7 → ~178 s), and every sample failure stays selected** (checked: 1434 ⊂ 535z's new set, and the rest fail nothing).

Hidden-set estimate: a module-level or import-fn edit in a dialect file costs **15.3 s on average now, 11.2 s with P1 and 6.5 s with P1+P2**, averaged over all 33 dialect families (bigquery, mysql, postgres, snowflake and spark: about 27.5 → about 10). At roughly 7-8 such commits in about 45 hidden commits, that is about 60 s.

Recall risk, by the patch shape that would cause a miss:
- **P1:** a dialect class-body edit that breaks only an optimizer fixture case read by a test whose strings do not equal the fixture stem or path tail. Today every reader of a header fixture is a `check_file("<stem>")` or `"optimizer/<stem>.sql"` test, and a fallback to substring keeps the old behaviour when exact matching finds nothing. Tests that really execute the dialect stay in family file_cov. **Low.**
- **P2:** a class-body edit that makes `Generator()` construction leak per instance without raising anywhere else, or breaks generation only for DAX or PRQL while their own test files pass. **Low.** generator_memory stays selected through coverage whenever `Generator.__init__` or `generator.py` changes, and through `dialect.py` edits (kept by the gate).
- **P3:** a parse-time builder that really caches through some other route (a module dict written under a different pattern). The cache-store detector covers the one present case (`_DISPATCH_CACHE`). Globals, `nonlocal`, `cls.` writes and cache decorators stay seeded. **Low.**
- **P4:** an expression class-body edit read by code that neither names the class, nor subclasses it, nor calls it by SQL name. An example is reflection over `Expression.__subclasses__`, and such loops are not in today's file_cov either, because class bodies run only at import. **Low-medium.**
- **P5:** a new nested helper whose name is used through `getattr` on a module (none in sqlglot). For the family intersection: a test that uses dialect X while executing no line of X's files and naming no X string. **Low.**

## (c) Code changes

**P1** in selector.py, replacing `fixture_tests` (:338-344):
```python
    def fixture_tests(self, fixture_rel: str) -> set[int]:
        """Tests that load this fixture: a string equal to its stem or ending in its path below
        tests/fixtures ("optimizer/x.sql"). Substring matching only when nothing matches exactly
        ("optimizer" is inside "optimizer/tpc-ds/tpc-ds.sql")."""
        memo = self.__dict__.setdefault("_fx_memo", {})
        if fixture_rel not in memo:
            stem = Path(fixture_rel).stem
            tail = fixture_rel.split("tests/fixtures/", 1)[-1]
            exact = {n for n, ss in self.strings.items()
                     if any(s == stem or s.endswith(tail) or s.endswith("/" + stem) for s in ss)}
            memo[fixture_rel] = exact or {n for n, ss in self.strings.items() if any(stem in s for s in ss)}
        return set(memo[fixture_rel])
```

**P2** in selector.py `_import_time_change` (:405-412):
```diff
         if rel.startswith(DIALECT_DIRS) or rel == "sqlglot/dialects/dialect.py":
-            # ... (test_generator_memory included: ...)
             for tid in self.index.get("all_dialect_tests", []):
                 n = self.by_id.get(tid)
-                if n is not None:
-                    hit.add(n)
+                # Slow loop tests (test_generator_memory 6.7 s) add nothing for one dialect's
+                # import-time edit: an import or construction error also fails the cheap loops
+                # (test_enum imports every dialect) and the dialect's own tests, and class-body
+                # code cannot leak per Generator(). Kept for dialect.py, the shared base.
+                if n is not None and (n not in self.slow or rel == "sqlglot/dialects/dialect.py"):
+                    hit.add(n)
```

**P3** in tools/build_index.py, the import_fns block. Remove `once = once or name.startswith("_build")` and seed cache-stored builders after `scan(tree)`:
```python
        # Cache-gated builders: f(...) whose result is stored into a module-level dict, directly
        # (CACHE[k] = f(...)) or through a local (v = f(...); CACHE[k] = v). Example: generator.py
        # _build_dispatch -> _DISPATCH_CACHE. Builders placed in tables by reference run per parse.
        module_names = {t.id for s in tree.body if isinstance(s, (ast.Assign, ast.AnnAssign))
                        for t in (s.targets if isinstance(s, ast.Assign) else [s.target]) if isinstance(t, ast.Name)}
        def stores_module(t) -> bool:
            return isinstance(t, ast.Subscript) and getattr(t.value, "id", None) in module_names
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            local_call: dict[str, str] = {}
            assigns = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)]
            for n in assigns:
                if isinstance(n.value, ast.Call) and callee(n.value):
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            local_call[t.id] = callee(n.value)
                        elif stores_module(t):
                            seeds.add(callee(n.value))
            for n in assigns:
                if isinstance(n.value, ast.Name) and n.value.id in local_call and any(map(stores_module, n.targets)):
                    seeds.add(local_call[n.value.id])
```
The index is rebuilt in `docker build`, so this costs no runtime CPU.

**P4** in selector.py `_import_time_change`. Skip the module file_cov for an expression class when the class signal exists:
```diff
     def _import_time_change(self, rel, line, extra_names=frozenset()):
-        family = self.dialect_family(rel)
-        hit: set[int] = set()
-        for f in family:
-            hit |= self.file_cov(f)
+        family = self.dialect_family(rel)
+        cls = self.top_class(rel, line)
+        hit: set[int] = set()
+        if cls and rel.startswith("sqlglot/expressions/"):
+            # One expression class's body is seen through the class, its subclasses and its SQL
+            # names, not through the other classes sharing the file.
+            by_name = self.sql_name_tests(cls, extra_names)
+            if by_name is None:
+                return set(self.all)
+            hit = self.class_tests(cls) | by_name
+            if hit:
+                return hit
+        for f in family:
+            hit |= self.file_cov(f)
         ...
-        cls = self.top_class(rel, line)
```
The rest of the function stays as it is, so an empty class signal falls through to file_cov as today.

**P5** in selector.py. Use only class-level methods, and keep dialect overrides inside the dialect's users:
```diff
 def def_names(tree):
     def visit(node, cls):
         for child in ast.iter_child_nodes(node):
             if isinstance(child, ast.ClassDef):
                 visit(child, child.name)
             elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
-                out.add((cls, child.name))
+                if isinstance(node, ast.ClassDef):  # methods only: locals and module functions
+                    out.add((cls, child.name))       # neither override nor get looked up by name
                 visit(child, cls)
```
And in `sqlglot_change` (:574-579):
```diff
-            out |= self.new_method_tests(new_names)
+            extra = self.new_method_tests(new_names)
+            if rel.startswith(DIALECT_DIRS) and Path(rel).stem not in ("__init__", "base"):
+                fam = self.dialect_family(rel)
+                users = set().union(*(self.file_cov(f) for f in fam)) | self.name_tests(
+                    {Path(f).stem for f in fam} - {"__init__", "base"})
+                extra &= users  # an override only reaches this dialect and its subclasses
+            out |= extra
```

## (d) Ranked recommendations (cost per unit of protection)
| # | rule | protection on the sample / hidden shape | change | s saved (sample) | risk |
|---|---|---|---|---|---|
| 1 | test_generator_memory via all_dialect_tests | none; fully redundant with test_enum and the dialect's own tests | P2 | **19.8** (hidden: about 3-7 s per dialect module edit) | Low |
| 2 | fixture-header dialects (substring) | none; a matching bug pulls in TPC-DS and TPC-H | P1 | **14.8** (hidden: about 15 s per big-dialect module edit) | Low |
| 3 | `_build` prefix in import_fns | negative: sends about 120 parse-time builders through the wide import rule | P3 | 0.3 (about 7-25 s per hidden builder edit without P1/P2) | Low |
| 4 | file_cov(expressions module) on class-body edits | none; class_tests and sql_name already give the readers | P4 | **4.7** (hidden: functions.py/query.py about 21-23 s each) | Low-med |
| 5 | new-def over every same-named def, nested included | no sample use; all tests for common names | P5 | 0 (hidden: up to about 30 s when it fires) | Low |
| 6 | dialect family file_cov, subclass rdeps, name strings, class_tests, `<lower>_sql`, lambda rules | the real signal (535z, ktz3, 6ze8) | keep | – | – |

Apply 1-3 together first: they are independent of one another and of the slow-test and split work.
