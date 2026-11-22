# 07: Patch forensics. How the hidden commits are probably made, and what to optimise for

Scope: all 30 `sample/commits/*.patch`, read alongside `sample/answers.json` (kind and failure count only; I used it to sanity-check, not to build a table),
`maps/impact.json.gz` line coverage, and the pinned source in `work/sqlglot`. Nothing was executed.

## (a) Findings

### F1. The commits are synthetic mutations, not upstream sqlglot commits
Evidence:
- Every code edit is a **one-line, same-indent replacement** in one hunk with git's default 3 lines of context (for example 2haz, 2jrq, 2qn5, 4d63, 535z, 6ze8, 8y7j, fryt, gbav, j6uz and ktz3). No code commit adds or deletes a line. No commit adds a test, touches a test `.py`, or changes more than one line per file.
- Fixture edits produce SQL no human would write: `ROW_NUMBER() 1 OVER` (986c), `ON xa .b` (2vdj), `STRUCTINT>` (bvw2), `x + 42 3` (egys), `SELECT *  x` (f4bn, `FROM` deleted), `0 WHERE` (mthh), `AS y42 ;` (st8f).
- The comment lines come from a small stock pool: "Cheap check first" (9u5k, 986c), "Kept for backwards compatibility" (f4xa, rszg), "Intentionally not cached" (986c), "Keep in sync with the generator" (bap8), "Mirrors the behavior of the reference implementation" (bvw2).
- The `index` lines carry real blob hashes, and patches against the same file share the base blob (clickhouse.py `e4335f0` in 2qn5, 8y7j and bj7g). The generator therefore edits a pinned worktree and runs `git diff`, one edit per worktree. This matches the README's "Each stands alone".

### F2. The mutation operators, and whether each was caught
Seventeen code mutations: the 16 `code` commits plus the code part of the multi commit rszg. The line coverage is the impact.json set size for the mutated line.

| operator | commit | file:line | line cov | caught? |
|---|---|---|---|---|
| `and` -> `or` (precedence change in a multi-line condition) | 2jrq | optimizer/eliminate_joins.py:71 | 10 | yes (8) |
| | 3utz | serde.py:53 | 181 | yes (37) |
| | ary6 | parsers/bigquery.py:500 | 10 | yes (10) |
| | rszg | typing/snowflake.py:101 | 1 | yes |
| `not x` -> `x` | j6uz | parsers/bigquery.py:650 | 2 | yes (2) |
| index change `seq_get(args,0)` -> `1` | 6ze8 | parsers/dremio.py:111 | 1 | yes (1) |
| integer constant | 535z | dialects/tsql.py:17 `WEEK_OFFSET -1 -> -2` | module | yes (1) |
| | 4d63 | dialects/snowflake.py:133 `"u": (16..)->(17..)` | module | **no** |
| bool literal | bj7g | generators/clickhouse.py:791 | 2 | yes (1) |
| | gbav | parsers/singlestore.py:213 `target=True->False` | module | **no** |
| | ktz3 | expressions/ddl.py:82 arg_types `False->True` | 0 | **no** |
| | 5y72 | optimizer/eliminate_subqueries.py:176 `copy=False->True` | 3 | **no** |
| `==` -> `!=` | 8y7j | generators/clickhouse.py:55 | none | **no** |
| | fryt | parsers/dremio.py:39 | 3 | **no** |
| `<` -> `<=` | 2qn5 | generators/clickhouse.py:811 | 6 | **no** |
| `is` -> `==` | 2haz | dialects/dialect.py:1280 (enum) | 86 | **no** |
| `is None` -> `== None` | 66ks | executor/context.py:53 | 51 | **no** |

Kill rate by operator class: connectives, negation and index changes 6/6. Constants and bool literals 2/6. Comparison flips 0/5. The generator does not filter out equivalent mutants: 9 of the 16 `code` commits (56%) break nothing.

### F3. Why the 9 equivalent mutants survive (all from static reading)
1. **Provable no-ops (no test could ever catch them).**
   - 66ks: `x is None` -> `x == None`. sqlglot has only four `__eq__` overrides: expressions/core.py:837, dialects/dialect.py:217 and :1180, and executor/env.py:64. The first three return False for None. Only `reverse_key` (env.py:64) would raise, and it is used only as a sort key.
   - 2haz: `is` -> `==` against `NormalizationStrategy.UPPERCASE`. This is a str-mixin enum (dialect.py:137). It is equivalent only because every assignment to `normalization_strategy` is an enum member (dialect.py:1171, :1173, and all `NORMALIZATION_STRATEGY =` RHS).
   - ktz3: `"constraint": False->True`. `required_args` (expressions/core.py:110) is only read by `error_messages` (core.py:1322), which flags `None` or `[]`. The parser always passes a bool (parser.py:2704, :2739).
2. **Untested boundary or data value.**
   - 2qn5: tests use `version=23.8` and `24.1` (test_clickhouse.py:2302-2316), never exactly 23.12.
   - 4d63: no Snowflake test string contains a `\u` escape.
3. **Uncovered line.** 8y7j: clickhouse.py:55-56 has no line in the coverage map.
4. **Covered but weakly asserted.**
   - fryt: two-argument DATE_ADD/DATE_SUB still produce the same AST through `build_date_delta`, and no test has a CAST-interval or 3-argument form.
   - 5y72: copying a freshly built `alias_(table_(name))` gives the same tree.
   - gbav: `SHOW GROUPS FOR USER 'root'` is in test_singlestore.py:926 and still passes. I could not explain this statically.

Sanity check (allowed by the brief): for every caught non-module code mutant, the failing tests are a subset of the line coverage of the mutated statement. That gives 0 tests missing for 2jrq, 3utz, 6ze8, ary6, bj7g and j6uz. rszg's one extra failing test comes from its fixture part.

### F4. Fixture edits
Each fixture edit is a single-line token mutation on one line of a `tests/fixtures/optimizer/*.sql` file. The kinds seen are:
- an inserted token: `42`, `0`, `1`, `NULL` or `x`;
- a deleted token: `FROM` or `<`;
- a space or letter shuffle.

The mutated line is an input line in some commits and an expected-output line in others. All 11 fixture edits (8 `fixture` commits plus the fixture parts of 3 multis) break exactly one test, the fixture's reader. None is equivalent, and that is expected: almost any change to the text of an expected line breaks the string comparison.

The sample only touched optimizer pair files. Hidden commits may also hit `identity.sql`, `pretty.sql` or `tpc-ds.sql`. For `tpc-ds.sql` the 21.5 s `test_execute_tpcds` really is needed: research/06-fixtures.md F1 lists those readers.

### F5. Comment and multi commits
- **Comment commits** insert one `#` line at the indentation of the next line. The insertion point can be a statement inside a function (9u5k), between imports (986c, rszg, bvw2), before a `def` (f4xa), or between a `def` line and its docstring (bap8). The AST is unchanged, and the selector already returns `[]` (selector.py:457-475, why.log).
- The one realistic way a comment insertion causes harm is an insertion *inside* a triple-quoted string, which changes a constant. The AST diff already catches that, because the dump changes.
- **Multi commits** apply 2-3 independent edits to different files, drawn from the same generators: 986c (comment + comment + fixture), bvw2 (comment + fixture), rszg (comment + code + fixture). The failing set is the union of the parts. All 3 sample multis contain a fixture edit and so fail. The selector's per-file union (selector.py:642) is the right model.

### F6. Inferred hidden distribution (about 45 commits, 1.5x)
Assuming the same generator and mix:
- 24 code, of which about 13 break nothing;
- 12 fixture, each breaking 1 test;
- 4-5 multi, almost all failing;
- 4-5 comment, all breaking nothing.

That gives about 17-18 zero-fail commits (about 40%).

On the sample, the 9 zero-fail code commits cost about **111 s of 217.7 s** (2haz 25.5, 4d63 27.5, 66ks 24.8, gbav 10.1, fryt 9.2, ktz3 7.1, and about 6.8 for 2qn5, 5y72 and 8y7j). On the hidden set that profile projects to about 165 s. Fixture and comment commits are already near their floor (about 2-3 s and 0 s).

Two operator facts matter for the hidden set:
- Among sqlglot's candidate sites there are 465 `is None` / `is not None`, 226 `copy=` and 13 `is Enum.MEMBER`. A mutator that picks `is`->`==` will mostly hit None comparisons.
- Comparison flips were 0/5 caught in the sample, so the hidden set probably has about 7 equivalent comparison or constant mutants.

## (b) Savings and recall risk
- **R1 (None canonicalisation):** 66ks goes from 24.8 to 0, saving **24.8 s** on the sample and probably 1-3 commits (25-50 s) on the hidden set.
  - Miss shape: `x is None` -> `x == None` where `x` can be a `reverse_key`, or an object from a non-sqlglot library whose `__eq__` misbehaves (numpy, pandas). Neither appears on any sqlglot `is None` line I checked. Risk: **very low**.
- **R2a (pure-Enum identity):** 0 on the sample. It is safe because a plain `Enum` without `__eq__` compares by identity, and any left operand's `__eq__` (the four above) returns False for an enum. Risk: very low.
  - Excluded on purpose: `TokenType` (IntEnum, tokenizer_core.py:15), where `1 == TokenType.X` can be true, and the str-mixin enums (dialect.py:98, 137, 160, 173).
- **R2b (str-mixin enum with provenance check):** 2haz, **25.5 s**. Risk: **medium**.
  - It needs every assignment to the compared attribute, anywhere in sqlglot *and* tests, to have an enum-member or enum-constructor RHS.
  - Miss shape: a test or user-facing kwarg storing a raw string in that attribute. Today there is none for `normalization_strategy`: all test uses go through `get_or_raise` strings, which pass through `NormalizationStrategy(...)` at dialect.py:1173.
  - This is the rule most at risk of looking fitted to the sample. Ship it only behind the provenance check.
- **R3 (do not try to prove "test-inadequacy" equivalents):** for `==`/`!=`, `<`/`<=`, constants and bool literals there is no static proof. Returning `[]` there is only worth it when P(fail) < cost/300. For a 2-10 s selection that means under 1-3%, and the sample shows these operators get caught 2 times in 6 (constants and bools). Keep line-covering tests and make them cheap instead:
  - line-level rather than window-level slow-test pruning (already selector.py:513-521);
  - data-reachability for module-level table constants (research/01 and 03);
  - k=1 when the selection is small.
- **R4 (arg_types required-flag flip):** ktz3, about 5 s. Risk: medium.
  - When the value flips False->True, the tests that can fail are those that build that class without the arg. When it flips True->False, only tests asserting "Required keyword" can fail (tests/test_parser.py, test_transpile.py, dialects/test_dialect.py).
  - Proposed selection: `class_tests(C)` ∪ those three files' tests that name C or one of its SQL names.
  - Miss shape: a builder that omits the arg reached from an unrelated test. Leave it to research/05-expressions to cost.

## (c) Code changes (selector.py)

Add a canonicaliser near `strip_docstrings` (after selector.py:789):

```python
# __eq__ overrides in sqlglot (core.py:837, dialect.py:217/:1180) return False for None; env.py:64
# reverse_key is only a sort key. So `x is None` and `x == None` agree for every sqlglot value.
PURE_ENUMS = frozenset({"ErrorLevel", "PropertiesLocation", "DType", "IsPredicatePrecedence",
                        "TrieResult", "ScopeType"})  # Enum/AutoName without str/int mixin (grep ^class .*Enum)

class _Canon(ast.NodeTransformer):
    """Rewrite provably-equivalent identity comparisons to equality, so ast.dump ignores them."""
    @staticmethod
    def _neutral(node) -> bool:
        if isinstance(node, ast.Constant) and node.value is None:
            return True
        # E.M or exp.E.M where E is a plain Enum
        return (isinstance(node, ast.Attribute) and isinstance(node.value, (ast.Name, ast.Attribute))
                and (node.value.id if isinstance(node.value, ast.Name) else node.value.attr) in PURE_ENUMS)

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) == 1 and isinstance(node.ops[0], (ast.Is, ast.IsNot)) and \
                (self._neutral(node.comparators[0]) or self._neutral(node.left)):
            node.ops = [ast.Eq() if isinstance(node.ops[0], ast.Is) else ast.NotEq()]
        return node
```

Then in `python_change`, extend the equality ladder (selector.py:458-467) with a third rung. It runs after the docstring rung and does not apply under `tests/`, so test files keep their existing path:

```diff
                 same = ast.dump(old_tree) == ast.dump(new_tree)
                 how = "docstring-only"
+            if not same and rel.startswith("sqlglot/") and rel != "sqlglot/executor/env.py":
+                c_old, c_new = _Canon().visit(old_tree), _Canon().visit(new_tree)
+                same = ast.dump(c_old) == ast.dump(c_new)
+                how = "identity-equivalent"
             if same:
```

Note: the docstring rung mutates the trees in place, and that is harmless here because every rung only compares. `bytes_inert` still guards the `same` branch.

R2b, optional and medium risk: allow a str-mixin enum `E` only when an index built at map time (tools/build_index.py) records that every `Assign`/`AnnAssign` to the compared attribute name, across `work/sqlglot/sqlglot` and `tests`, has an RHS of the form `E.M`, `E(...)` or `self.<UPPER_CONST>` whose own assignments are all `E.M`. Pass the allowed `(attr, E)` pairs into `_Canon` and require the other operand to be `self.<attr>`. If any assignment fails the check, fall back to normal selection.

## (d) Ranked recommendations
1. **R1 None canonicalisation:** about 24.8 s on the sample (25-50 s projected on the hidden set). Risk very low. About 20 lines.
2. **Focus effort on zero-fail code commits** (about 51% of sample cost, projected about 165 s hidden), through cheap insurance selections rather than proofs:
   - line-exact coverage for slow tests;
   - data-reachability for table constants;
   - k=1 for small selections.

   This is up to about 60 s, delivered through other notes (01/03/11/12). Risk depends on each rule.
3. **R2a pure-Enum identity:** 0 s on the sample, a small amount hidden. Risk very low. Free with R1.
4. **R2b str-mixin enum with provenance:** 25.5 s on the sample. Risk medium. Build the provenance index first, and drop the rule if the index cannot show every assignment.
5. **R4 arg_types flag flips:** about 5 s. Risk medium.
6. **Do not** return `[]` for comparison flips, constants or bool literals without a proof. Two of six constant and bool mutations failed in the sample, and one miss (300 s) outweighs years of 2-10 s savings.
7. Keep fixtures as they are (1 reader test each, all 11 caught) and comments as they are (`[]`). Make sure `tpc-ds.sql` and `identity.sql` edits map to their full reader lists (research/06 F1).

Summary:
1. The hidden patches are synthetic, git-diffed single-line mutations, as described in F1-F5.
2. Kill rate: connective, negation and index flips 6/6. Comparison flips (`is`/`==`/`!=`/`<=`) 0/5. Constants and bools 2/6. 56% of code mutants break nothing.
3. Zero-fail code commits are about 111 of 217.7 s on the sample (projected about 165 s hidden), so the selector should optimise them first.
4. Only identity-to-equality rewrites are *provably* equivalent. Canonicalising `is None`/pure-Enum saves 24.8 s at very low risk; the str-enum provenance variant saves 25.5 s more at medium risk.
5. For everything else, keep the line-covering tests and make them cheap. Never return `[]` without a proof.
