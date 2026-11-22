# 19 - Hand trace of the current selector.py on 12 sample patches

Method and caveats
- I did not run `selector.py`, `score.py`, `tools/build_index.py` or anything of sqlglot. I read `selector.py` and inspected `maps/impact.json.gz` with read-only Python. I also rebuilt the static index (toplevel, lambdas, refs, subs, rdeps, fns) in memory from `work/sqlglot` with an `ast` and `tokenize` re-implementation of `tools/build_index.py`. `maps/index.json` does not exist locally, so the numbers below assume the Docker-built index equals mine. The scratch code is in the session scratchpad under `w19/`, not in the repo.
- Counts are tests selected out of 1620. "cost" is `max load + 2 s * k` using the same greedy longest-first packing as `split()` (selector.py:463-481).
- Answers were used only to check recall. No rule below is tuned to them.

## (a) Findings: per-patch trace

| patch | edit | rule (selector.py line) | tests | s of tests | slow pulled | failing in answers | cost |
|---|---|---|---|---|---|---|---|
| 4d63 | `dialects/snowflake.py:133` constant in nested `Tokenizer` class body | module / `import_time_change` (:318-326, :250-261) | 357 | 11.0 | none | none (0 expected) | 9.5 s, k=2 |
| 535z | `dialects/tsql.py:17` module constant `WEEK_OFFSET` | module (family tsql+fabric) | 168 | 7.8 | none | 1434 test_simplify, included | 7.9 s, k=2 |
| 6ze8 | `parsers/dremio.py:111` inside `"REGEXP_SPLIT": lambda` | lambda (:311-316), range 110-115 | 5 | 0.0 | none | 302 included | 2.0 s, k=1 |
| ktz3 | `expressions/ddl.py:82` `arg_types` of `TriggerProperties` | module (family = file only) | 134 | 6.3 | none | none | 7.1 s, k=2 |
| gbav | `parsers/singlestore.py:213` dict entry in `SHOW_PARSERS` (not a lambda) | module | 37 | 1.4 | none | none | 3.4 s, k=1 |
| 2haz | `dialects/dialect.py:1278` `is` to `==` in a method | fn (:299-309), window 1276-1280 | 102 | 36.8 | tpcds, tpch | none | 25.5 s, k=2 |
| rszg | `typing/snowflake.py:101` `and` to `or`, plus a `.sql` fixture edit and a comment in scope.py | fn + typing-test rule (:333) + fixture-stem; scope.py is ast-identical | 4 | 3.8 | none | 1295, 1355, 1382, all included | 5.8 s, k=1 |
| 986c | comment in `dialects/singlestore.py` and in `typing/snowflake.py`, plus a `.sql` fixture edit | ast-identical x2 (:283-284) + fixture-stem | 1 | 0.3 | none | 1454 included | 2.3 s |
| 9u5k | comment line in `generators/sqlite.py` | ast-identical | 0 | 0 | none | none | 0 (empty) |
| bap8 | comment line in `transforms.py` | ast-identical | 0 | 0 | none | none | 0 (empty) |
| ary6 | `parsers/bigquery.py:500` `and`/`or` swap in a method | fn window | 10 | 2.4 | none | all 10 included | 4.4 s |
| 2jrq | `optimizer/eliminate_joins.py:71` `and`/`or` swap | fn window | 33 | 33.2 | tpcds, tpch | all 8 included (1106 tpcds is itself a failing test) | 25.5 s |

Details behind the table
- 4d63: nothing in `Tokenizer` is a function, so `innermost()` gives `fn=None`. No lambda exists in snowflake.py, so the edit goes to `import_time_change`. `dialect_family` returns the snowflake dialect, parser, generator and typing files. The result is the union of their whole-file coverage (229+272+227+15 tests), 344 tests that name `snowflake` (test file or string), and `class_tests("Snowflake")` (4 tests). `top_class` returns `Snowflake`, since the nested `Tokenizer` is inside it.
- 535z: the family is tsql plus fabric (a subclass found through rdeps). 1434 `test_simplify` comes from file coverage of the tsql dialect, parser, generator and typing files, not from name matching.
- 6ze8: `lambda_range` finds (110, 115). The edit line 111 is inside it, `cov()` returns 5 tests, and the lambda branch `continue`s with no module fallback.
- ktz3: `ddl.py` is not in a dialect directory, so the family is `{ddl.py}`. Whole-file coverage is 128 tests. `class_tests("TriggerProperties")` adds only 7. This is about 20x wider than the class-specific set, but cheap here.
- gbav: the `_show_parser(...)` call is a plain dict value, so there is no lambda. The family is singlestore, which has no subclasses. The result is 37 tests, with no slow tests.
- 2haz: this is the old fn rule. It is semantically a no-op (`is` vs `==` on an enum), but AST-different. The cost is the two executor TPC tests.
- rszg: the edit hits `_annotate_within_group` (typing/snowflake.py:91-108). Window 99-104 gives `test_optimize` and `test_annotate_funcs`. `+test_typing` adds 1. The fixture rule matches the stem `qualify_columns_ddl` to one test, `test_qualify_columns`, which is the 1382 failure. Without the fixture rule 1382 would be missed. `scope.py` is only a comment, so it is ast-identical.
- 986c: all three Python files are ast-identical (the typing comment returns `set()` before the :333 typing rule). The fixture stem `unnest_subqueries` gives 1 test, the failing one.
- 9u5k, bap8: ast-identical gives an empty output, which is correct (kind "comment", no failing tests).
- Totals for these 12 patches: 0 misses and 0 failing tests lost. The sum of per-commit costs is about 87 s. Of that, 2haz and 2jrq are 51 s. The other ten patches cost about 36 s together.

No new misses on this sample. No 2026-11-21 rule produced a cost regression larger than about 10 s on the 12 patches. The two expensive ones (2haz, 2jrq) go through the old fn rule.

## (b) Recall risks and cost waste

R1. **Adjacent-lambda insertion (recall, new in 11-21).** The lambda branch (selector.py:311-316) returns the lambda's own coverage and `continue`s. `touched_old_lines` (:81-91) marks both neighbours of a pure insertion. If a new table entry is inserted between two adjacent lambdas, both neighbours sit inside lambda ranges and the module fallback never runs. Examples are a new `TRANSFORMS` entry in `generator.py`, a new `FUNCTIONS` entry in `parser.py`, or a new entry in `parsers/snowflake.py`. The selection is then the coverage of the two neighbour lambdas. The tests that use the new key (the previous behaviour was Anonymous, or the default `<x>_sql`) are not selected. I counted 956 adjacent lambda pairs in the repo (e.g. generator.py 139, parser.py 305, parsers/snowflake.py 73). If the new entry sits next to a non-lambda entry, the other neighbour falls to the module rule and is safe. `changed_statement_lines` does not rescue this: it skips widening when all touched lines are inside lambdas (:452-455), and it skips tables of 40 lines or more (:447-448).

R2. **Class-body or module-level edits in shared modules are very wide (cost).** `import_time_change` takes `file_cov(rel)` for non-dialect modules, so any line any test executes in that file counts. Measured per file, with the tests selected and the slow tests pulled:
- `helper.py`, `tokens.py`, `errors.py`, `expressions/core.py`, `parser.py`, `__init__.py`, `dialects/dialect.py`: 1500-1590 tests, about 96-103 s, all slow tests.
- `transforms.py`: 788 tests (51 s, tpcds, tpch, opt_tpch). `optimizer/scope.py`: 732 (60 s). `expressions/builders.py`: 676. `expressions/datatypes.py`: 621.
- 13 files reach 1000 tests or more, and 78 of the 184 sqlglot files pull at least one slow test.
For constant or import-line edits (say `UNMERGABLE_ARGS`), the better selection is the tests that run the lines naming that constant. This is a big cost for the rule's recall gain, but the rule is the sound fallback.

R3. **Small dialect modules pull `test_generator_memory` (6.7 s).** `test_generator_memory` is the first importer of lazy dialects, so import-time lines of dax, athena, trino, bigquery, hive, mysql and presto are credited to it. A 4-line `dax.py` edit selects 7 tests (6.7 s, of which 6.7 s is gen_mem); a `dialects/mysql.py` module edit selects 449 tests including gen_mem. That is legitimate for class-body edits (a leak is possible), but not for edits to a constant. Singlestore, snowflake, tsql and sqlite do not have this effect.

R4. **Docstring-only edits** are not `ast.dump`-identical (the docstring is a Constant), so they take the fn rule (e.g. 2haz-style cost, 30-40 s if a hot function). `grep __doc__` finds only sqlglot/expressions/core.py:112, which normalises a missing class docstring to "", so dropping docstrings before the compare is safe. This is a general cost saver, not a miss risk.

R5. **Whole-file coverage in `ddl.py`-style modules** (ktz3: 134 vs 7 for the class itself) is cheap enough. It could be tightened, but the cost is under 7 s, so I would not spend effort here.

R6. Not a new risk but relevant: `tests/test_typing.py` compares code objects, not text, so skipping it for comment-only typing edits (986c) is sound.

## (c) Proposed code changes

C1. Close the adjacent-lambda hole (selector.py). In `touched_old_lines`, also return which lines are only insertion neighbours. In `sqlglot_change`, use the lambda branch for a line only if it is a removed or modified line, or an insertion point strictly inside that lambda.

```diff
--- a/selector.py
+++ b/selector.py
@@ def touched_old_lines(f: dict) -> set[int]:
+def insertion_points(f: dict) -> set[int]:
+    """Old line numbers that a pure insertion sits in front of."""
+    return {old_no for h in f["hunks"] for tag, _t, old_no in h["lines"] if tag == "+"}
+
+
+def removed_lines(f: dict) -> set[int]:
+    return {old_no for h in f["hunks"] for tag, _t, old_no in h["lines"] if tag == "-"}
@@ def sqlglot_change(self, f, old_tree, new_tree)
         changed = changed_statement_lines(old_tree, new_tree, touched_old_lines(f))
+        ins, rem = insertion_points(f), removed_lines(f)
@@
-                lam = self.lambda_range(rel, ln)
+                lam = self.lambda_range(rel, ln)
+                if lam and ln not in rem:
+                    # An insertion's neighbours count only when the insertion is inside this lambda.
+                    if any(ln in (p - 1, p) and not (lam[0] <= p - 1 and p <= lam[1]) for p in ins):
+                        lam = None
```

With this change, an entry inserted between two lambdas falls through to `import_time_change` (the family rule), which is the previous safe behaviour. A line inserted inside a multi-line lambda body keeps the narrow rule.

C2. Narrow constant and import-line edits (tools/build_index.py plus selector.py), allowed since it is static analysis of the pinned source. Index `names`: for every top-level `Assign`/`AnnAssign` target name, the `[file, line]` of all `NAME` tokens with that string, across `sqlglot/`. In `import_time_change`, if the changed statement is a simple top-level assignment (not a class body), take `cov` over those lines instead of `file_cov(rel)`. This is the biggest potential cost saver for module-level edits (R2). Keep the class-body and dialect paths unchanged. Risk: tests that exercise a constant only at import are not attributable, so keep the old whole-file fallback when this set is empty.

```python
# tools/build_index.py, inside build(), after refs is built
names = {}
for rel, tree in trees.items():
    for node in tree.body:
        for t in (node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []):
            if isinstance(t, ast.Name):
                names.setdefault(t.id, [])
tokrefs = {}
for rel, text in texts.items():
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type == tokenize.NAME and tok.string in names:
            tokrefs.setdefault(tok.string, []).append([rel, tok.start[0]])
# add "namerefs": tokrefs to the returned dict
```

C3. Strip docstrings before the ast-identical compare (selector.py:283):

```python
def strip_docs(tree):
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body \
           and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant) \
           and isinstance(n.body[0].value.value, str):
            n.body = n.body[1:] or [ast.Pass()]
    return tree
# use: ast.dump(strip_docs(old_tree)) == ast.dump(strip_docs(new_tree))  (on copies)
```

## (d) Ranked recommendations

1. **C1 (adjacent-lambda insertion).** The only new recall hole I found in the 11-21 rules. The fix is about 8 lines, with a small cost increase for insertion patches only. High value if mutation or hidden commits add dict entries.
2. **C2 (constant and import-line narrowing).** The largest potential cost saver. A module-level edit in transforms, scope, builders or datatypes costs 25-35 s today (R2). It matters only for hidden patches of that shape; none of the 12 here hit it. Medium value, moderate effort (index plus selector change).
3. **C3 (docstring-only edits to empty).** Saves up to 25-35 s on a docstring edit in a hot function. Low effort and low risk.
4. Keep the rest of the 11-21 rules as they are. On these 12 patches they give 0 misses, with sizes of 5 to 357 tests for dialect and class edits and no slow tests. The module rule for dialect edits (4d63, 535z, gbav) costs 3-10 s each, an acceptable price for recall.
5. When `selector.py` is next run, check that `maps/index.json` is present in the image. Without it, `import_time_change` falls back to file coverage only (selector.py:145-149) and the dialect family expansion disappears (family = `{rel}` plus same-stem files).
