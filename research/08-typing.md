# 08 - sqlglot/typing/*.py (EXPRESSION_METADATA), annotate_types, tests/test_typing.py

## (a) Findings

1. **Structure.** `sqlglot/typing/__init__.py:60` defines the base `EXPRESSION_METADATA` dict. It also defines helpers `annotate_by_numeric_arg` (:15), `annotate_int_as_double` (:33) and `TIMESTAMP_EXPRESSIONS` (:51). Each dialect file builds its dict with `**parent` spreads:
   - hive imports the base (`typing/hive.py:6,24`).
   - spark2 imports hive (`typing/spark2.py:7`). spark imports spark2 (`typing/spark.py:4,8`). databricks imports spark (`typing/databricks.py:4,7`).
   - redshift imports postgres (`typing/redshift.py:6,34`).
   - snowflake, bigquery, duckdb, mysql, tsql, presto, clickhouse, sqlite and postgres import the base.
   - Entries are plain dicts of the form `{"returns": DType}` or `{"annotator": fn}`.
2. **Single consumer.** The only reader of the data is `optimizer/annotate_types.py:207`, which sets `self.expression_metadata = ... or dialect.EXPRESSION_METADATA`. The dispatch is at `annotate_types.py:543-549`: `spec = self.expression_metadata.get(expr.__class__)`, then `annotator(self, expr)` or `_set_type(expr, returns)`.
   - Each dialect copies the dict (`dialects/snowflake.py:16,52`, and the same pattern in every dialect). `dialects/dialect.py:35,1083` copies the base for dialects with no typing file.
   - Dialects without their own typing file (doris, starrocks, singlestore, trino, athena, ...) inherit the class attribute from their parent dialect class. They import the parent dialect module, so `rdeps` of the parent dialect file reaches them.
3. **Dict edits are invisible to line coverage.**
   - A dict entry is a module-level statement that runs once at import.
   - In `maps/impact.json.gz`, `typing/snowflake.py` has 390 mapped lines. 384 of them map to set 417 or similar import-time sets.
   - Set 417 holds only 4 `test_optimizer` tests, because import is attributed to whichever test imported first. Function-body lines map to the 15 tests that ran the annotators.
   - So for a `returns` entry, coverage says almost nothing about who is affected. The real consumers are every test that reaches line 543.
4. **Tests that reach `annotate_types.py:543`.**
   - Measured from the map: set 8049 holds 307 tests and 50.9 s. They are test_optimizer 110, test_executor 54, test_snowflake 23, test_bigquery 14, test_duckdb 12, and the rest across other dialect files.
   - The two tests with the most direct dependence on the metadata are `test_optimizer::test_annotate_funcs` (1.96 s, a loop over `annotate_functions.sql`, which has 846 snowflake, 390 bigquery, 175 mysql, 99 hive etc. cases, each with its own dialect) and `test_annotate_types`.
   - Next come the dialect tests that call `annotate_types` (test_snowflake 34 mentions, duckdb 17, bigquery 16, teradata 14, postgres 8). The optimizer paths that call it implicitly also depend on the metadata: `canonicalize`, `simplify`, `qualify_columns` and `resolver` all use `.type`. These add test_executor and the others.
5. **`tests/test_typing.py:55-83` is the only test that reads the typing files as source.**
   - `test_no_redundant_expression_metadata` imports every module and compares each child's *explicitly redefined* entries with the parent's. For annotators it uses `_same_code` (bytecode, names, varnames and consts).
   - It fails when a child entry is equal to the parent's but not the same object. That can be caused by editing the **parent**: a base or hive edit that makes spark2's explicit entry identical to it.
   - It takes 0.011 s (`sample/tests.json`), so always selecting it costs nothing.
   - It cannot be broken by anything outside `sqlglot/typing/`. It does not touch `annotate_types`.
6. **Current selector handling** (`selector.py:333-334`, `:23-24`, `:205-220`, `:239-258`, `:312-325`):
   - For any non-ast-identical change in `sqlglot/typing/*`, `TYPING_TEST` is added at the end of `sqlglot_change`. This is correct. The early returns (`package-init` and the other fall-backs to `all`) already include it.
   - Function-body edits use per-function line coverage. That is precise.
   - Dict-entry and constant edits go through `import_time_change`. It unions `file_cov` of the dialect family (`typing/x`, `dialects/x`, `parsers/x`, `generators/x`, `tokenizers/x`, plus importers) and `name_tests` (any test file `test_x.py` plus tests holding the string `"x"`). `top_class` is `None` in typing files, so there is no `class_tests` step.
   - A dict edit in `typing/__init__.py` hits `selector.py:320-322` and returns **all tests (103 s)**.
7. **Sample rszg.** The edit is `and` to `or` inside `_annotate_within_group` (a function in `typing/snowflake.py`). It takes the `fn` coverage path and `TYPING_TEST` is added. The path is correct.
8. **Sample 986c.** The typing change is a comment-only line inside `_annotate_median`. `ast.dump` is equal, so it returns `set()` with reason `ast-identical` and nothing is selected for this file. Correct, since comments cannot affect behavior. (The other file in the same patch, `dialects/singlestore.py`, is also a comment only.)

## (b) Recall risks and cost waste

Recall risks. I found no shape that would miss, but there are two thin spots:

- **R1 (thin).** A dict-entry or constant edit in `typing/X.py` is covered only through `file_cov(family)` plus `name_tests`.
  - The family does include dialects that import X, through `rdeps` of both the typing files and the dialect files.
  - It misses a test that annotates a hand-built AST with `dialect="x"` and never runs a line of x's parser, generator or tokenizer, and whose strings do not hold `x` as a whole string (for example `dialect=f"{name}"`).
  - It also misses a test that reaches the dialect through `Dialect.get_or_raise(var)`. These shapes are rare, since the tests read fixtures that name the dialect, so it is a low risk.
  - R1 is closed by the annotator-consumer rule below, which goes through `Dialect.EXPRESSION_METADATA`. The instantiation of the dialect class would have to be seen, and that is not seen by line coverage either. Keep `name_tests` and the string-set as the guard.
- **R2 (none today).** Adding a new typing file such as `typing/oracle.py` is `file-added-or-removed` and runs all tests. That is safe.

Cost waste:

- **W1 (largest).** A dict or constant edit in `typing/__init__.py` selects everything (103 s at `selector.py:320`).
  - Only tests reaching line 543 can observe it, plus `test_typing`. That is the 307 tests in set 8049 (50.9 s), about 50 s fewer.
  - Many of the 307 are cheap anyway. Selecting by file rather than whole test sets would not shrink it further here.
- **W2.** A dict edit in `typing/snowflake.py` takes the union of whole-family coverage and every test that holds the string "snowflake". That includes all of test_snowflake and many tests that never annotate.
  - Intersecting with the annotator consumers (set 8049) drops them. The annotating tests are the only ones that can observe a metadata entry.
  - On the sample commits touching typing, a dict edit in snowflake would shrink from about 13 files' worth of snowflake family coverage to roughly the 23 snowflake tests plus `test_annotate_funcs`, `test_annotate_types` and the optimizer paths on snowflake.
- **W3.** `name_tests` is whole test files, so `test_snowflake.py` is selected in full even for a one-entry edit.

## (c) Proposed code changes

The keying rule: **an entry keyed by `exp.Foo` in `typing/X.py` is observable only by tests that execute `annotate_types.py:543` while the active dialect is X or a subclass or importer of X**, plus `test_typing`.

Entry-level refinement (optional, see rule 3): the coverage of annotator code for Foo can narrow this further when the entry's type is `exp.Foo`.

Rule:

```
affected(edit in typing/X.py, entry exp.Foo) =
    ANNOTATORS                               # tests covering annotate_types.py line 543
    ∩ ( file_cov(dialect_family(X)) ∪ name_tests(words(family)) ∪ test_annotate_funcs ∪ test_annotate_types )
  ∪ TYPING_TEST
```

For X = `__init__` (base): every dialect inherits, so `ANNOTATORS ∪ TYPING_TEST` (no family intersection).

### Diff 1: `selector.py`, constants near line 24

```python
ANNOTATE_FILE = "sqlglot/optimizer/annotate_types.py"
ANNOTATE_DISPATCH = "spec = self.expression_metadata.get("   # looked up by text, not by line number
```

### Diff 2: `selector.py`, a new method next to `import_time_change` (~line 258)

```python
    def annotators(self) -> set[int]:
        """Tests that reach the metadata dispatch in TypeAnnotator._annotate_expression."""
        if getattr(self, "_annotators", None) is None:
            src = self.source(ANNOTATE_FILE) or ""   # pinned source text
            line = next((i for i, s in enumerate(src.splitlines(), 1) if ANNOTATE_DISPATCH in s), None)
            self._annotators = self.cov(ANNOTATE_FILE, [line]) if line else set(self.all)
        return self._annotators

    def typing_entry_change(self, rel: str) -> set[int]:
        """Class-body/module-level edit under sqlglot/typing: metadata is read only by the annotator."""
        ann = self.annotators()
        if Path(rel).stem == "__init__":
            return ann                              # every dialect inherits the base
        fam = self.dialect_family(rel)
        scope: set[int] = set()
        for f in fam:
            scope |= self.file_cov(f)
        scope |= self.name_tests({Path(f).stem for f in fam} - {"__init__", "base"})
        # The fixture-driven annotator tests loop over dialects inside one test; always keep them.
        scope |= {n for n in ann if "test_annotate" in self.ids[n]}
        return ann & scope
```

`self.source`, `self.cov`, `self.all` and `self.ids` are the existing helpers or fields. If the test-id list is called something else in `selector.py`, use that name. If `source()` returns `None`, the fallback is `set(self.all)`.

### Diff 3: `selector.py`, `sqlglot_change`, before the `package-init` return (~line 319)

```python
                if rel.startswith("sqlglot/typing/"):
                    out |= self.typing_entry_change(rel)
                    reasons.append("typing-entry")
                    continue
                # Class body or module level: runs at import, consumed anywhere the module is used.
                if rel.endswith("__init__.py"):
```

The `continue` keeps the later `TYPING_TEST` line at 333-334, which is what adds the redundancy check.

If `self.cov` does not accept a list, use `range(line, line + 1)` instead.

### Diff 4 (optional): entry-level narrowing

For a dict edit that touches a single `exp.Foo` key, parse the edited statement's key node. Pull the name after `exp.` from `ast.Attribute` keys inside the changed dict entry, including keys inside a `{expr_type: ... for expr_type in {exp.A, exp.B}}` comprehension set. Then `ann & (class_tests("Foo") ∪ tests whose strings contain the function's sql name)`.

This is not recommended as a default. `class_tests` is keyed on source references. Function nodes are built dynamically from `FUNCTION_BY_NAME` in the parser, so the references to `exp.Foo` are incomplete and recall could drop. If it is used, apply it only as a size cap: if `len(ann & scope) > N`, keep the entry-level result as additional info only.

## (d) Ranked recommendations

1. **Route `typing/__init__.py` module-level edits to `annotators() ∪ TYPING_TEST` instead of all tests** (Diff 3 for stem `__init__`). Saves about 50 s on any such patch. No recall loss: the metadata has one reader, `annotate_types.py:543`. Impact: high, since base-metadata edits are the commonest shape in this package.
2. **Intersect dialect typing dict edits with `annotators()`** (Diff 2 + 3). Removes the non-annotating members of the family and name sets. Likely cuts a snowflake-style dict edit by more than half. Risk: low, and covered by the R1 mitigations (name_tests, `test_annotate*`).
3. **Keep `TYPING_TEST` on every non-identical typing change** (already done). It is the only test that can fail on a redundancy edit. Cost is 0.011 s.
4. **Keep the function-body path as it is.** Per-function line coverage is precise for annotators (rszg is the model case).
5. **Keep `ast-identical` to empty for comments** (986c). Verified correct for typing files.
6. Entry-level (`exp.Foo`) narrowing: skip it unless the size cap is needed, per Diff 4.
7. Sanity check once the code is allowed to run (not in this task): confirm that set 8049 is non-empty and that `test_annotate_funcs` is a member. If the map never recorded line 543, `annotators()` falls back to `all`, which is safe but wasteful.
