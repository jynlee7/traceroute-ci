# 04 – Zero-fail walk-through A: 2haz, 4d63, 66ks (~78 s on the sample, 0 failures)

Nothing here was run. Evidence comes from reading the patches, the pinned source in `work/sqlglot/`, `maps/impact.json.gz`, `work/index.json` and `work/out.jsonl`. I inspected those with read-only Python one-liners.

| commit | patch | selected | cost | slow tests that drive the cost | why nothing fails |
|---|---|---|---|---|---|
| 2haz | `dialects/dialect.py:1280` `is NormalizationStrategy.UPPERCASE` → `==` | 101 | 25.5 s | test_execute_tpcds 21.5 | **Equivalent mutant** (enum identity vs equality) |
| 66ks | `executor/context.py:53` `self._table is None` → `== None` | 51 | 24.8 s | test_execute_tpcds 21.5 | **Equivalent mutant** (None identity vs equality) |
| 4d63 | `dialects/snowflake.py:133` NUMERIC_ESCAPES `"u": (16,…)` → `(17,…)` | 561 | 27.5 s | tpcds 21.5, generator_memory 6.7, tpch 3.9+3.5 | **Real behaviour change that no test reaches** (no test reads Snowflake SQL with a `\u` escape) |

## (a) Findings

### 2haz: an equivalent mutant
- `dialect.py:1270-1283` `case_sensitive()`: `unsafe = str.islower if self.normalization_strategy is NormalizationStrategy.UPPERCASE else str.isupper`.
- `NormalizationStrategy(str, AutoName)` (`dialect.py:137`) is a **str-mixin** enum. `==` and `is` give different answers only if `normalization_strategy` can hold a plain `str` equal to `"UPPERCASE"`, or a non-member.
- Every assignment to the attribute is an enum member:
  - `dialect.py:1171` `self.normalization_strategy = self.NORMALIZATION_STRATEGY`.
  - `dialect.py:1173` `NormalizationStrategy(normalization_strategy.upper())`. This is the path for user strings such as `"mysql, normalization_strategy = lowercase"` (`tests/dialects/test_dialect.py:251-329`, `test_bigquery.py:3741`).
  - All 16 class-body `NORMALIZATION_STRATEGY = ` assignments in `sqlglot/` are `NormalizationStrategy.X` (grep).
  - No test assigns `.normalization_strategy =` directly (grep of `tests/`).
- So the two operators are identical on every reachable value, and the patch is semantically a no-op.
- **Why it cost 25.5 s.** The changed line is a continuation line of a multi-line statement. `changed_statement_lines` widens it to 1278, which is not in `same_indent_replacements`, so the slow-test pruning (`selector.py:512`) never fires. Coverage of 1278/1280 really does include `test_execute_tpcds` (86 tests on that line set; it reaches the line via `qualify_columns.py:1361`). The tpcds test does run the line. It just cannot tell the two operators apart.

### 66ks: an equivalent mutant
- `executor/context.py:53` `if self._table is None:` → `== None`.
- `_table` is annotated `Table | None` (`context.py:33`). It is assigned only `None` (`:33`) and `list(self.tables.values())[0]` (`:54`), and the latter is an executor `Table`. Grep finds no other `_table =` in `sqlglot/executor`.
- `executor/table.py` defines no `__eq__`, so `Table() == None` falls back to identity and is `False`. The mutant is equivalent.
- The only `__eq__` definitions in `sqlglot/` are:
  - `expressions/core.py:837`: `self is other or (type(self) is type(other) and …)` gives `False` against None.
  - `dialect.py:217` (`_Dialect`) and `:1180` (`Dialect`) give `False` against None.
  - `executor/env.py:64` `reverse_key.__eq__` returns `other.obj == self.obj`. **It raises `AttributeError` against None.** This is the one hazard for a global `== None` rule.
  - `tests/` defines no `__eq__` (grep).
- **Why it cost 24.8 s.** Every line of `context.py` carries the TPC executor proxy coverage (plan.md 2026-11-20). For example, line 51 has a set of size 1 that holds only tpcds. So any executor edit pulls in the 21.5 s test.

### 4d63: a real change on a path no test reaches
- The `"u"` tuple's *values* are read in only two places:
  - `tokenizer_core.py:1240`, in `_scan_numeric_escape`, as `spec = self.numeric_escapes.get(...)`. This line is reached by 23 tests (4.18 s in total, no slow test).
  - `:1219`, in `_read_byte_escape`. Coverage has no record for this line. It is reached only with `NUMERIC_ESCAPES_ARE_BYTES`, which is `False` for Snowflake.
- Every other read of `NUMERIC_ESCAPES` looks only at keys or truthiness, so a value-only edit is invisible to it:
  - `dialect.py:404-410` (metaclass, import time): `"x" in numeric_escapes`. Snowflake has `"x"`, so the `"u"` entry is never consulted.
  - `generator.py:1723`: `bool(... NUMERIC_ESCAPES or ...)`.
  - `generator.py:3283`: `"x" in ...`.
  - `tokens.py:606`: a pass-through kwarg to `tokenizer_core.py:657`.
  - `tokenizer_core.py:1323`: a local alias, then `:1362` a truthiness check.
- The change is not equivalent. With base 17, `_DIGIT_VALUES.get(c, 16)` (`tokenizer_core.py:11,1207`) accepts the "non-hex" value 16 as a digit, and `A` decodes to 69, not 65. It is simply unobserved:
  - `tests/dialects/test_snowflake.py` has no `\u` escape. The only escape test is `:1241` `\\x21`.
  - `test_presto.py:777,786` emits `'☃'` *for* snowflake through `write=`. That is generator output, and the generator never reads the tuple values.
- **Why it cost 27.5 s.** The class-body rule (`_import_time_change`) unions:
  - `file_cov` of the Snowflake family;
  - `name_tests({"snowflake"})`;
  - `all_dialect_tests`, which pulls in `test_generator_memory` 6.7 s;
  - `fixture_dialects["snowflake"]`, which lists `optimizer.sql`. `fixture_tests` then matches the stem `"optimizer"` **as a substring** of `test_execute_tpcds`'s strings (`'/optimizer/tpc-ds/'`, `selector.py:343`). That is how tpcds 21.5 s and tpch 3.9 + 3.5 s get in.
- Not one of those tests reads the edited value.

## (b) General principles, savings and recall risk

### P1: Canonicalise operator-equivalent comparisons before the AST-identical check → `[]`
Rewrite `==` → `is` and `!=` → `is not` in both trees before `ast.dump`, but only when one operand is:
1. the constant `None`, and the module neither defines nor names a class whose `__eq__` is unsafe against None. At the pin that set is `{reverse_key}`, built at build time.
2. `E.M`, where `E` is a sqlglot Enum with no str/int mixin and no `__eq__`: `TrieResult`, `ScopeType`, `IsPredicatePrecedence`, and the pure `AutoName` subclasses `ErrorLevel`, `DType`, `PropertiesLocation`. For pure enums, `==` *is* identity.
3. `E.M` with a mixin enum (`NormalizationStrategy`, `Dialects`, `TokenType(IntEnum)`, …), **only if** the other operand is `<expr>.attr`, `attr` is "enum-typed", and no test assigns `attr`. `attr` is enum-typed when every assignment to it anywhere in `sqlglot/` is `E.M`, `E(...)`, or `self.Y`/`cls.Y` with `Y` itself enum-typed.

`True`, `False`, int or str literals are never canonicalised (`1 == True`, interning).
- **Sample saving:** 2haz 25.5 s + 66ks 24.8 s = **50.3 s**. No other sample commit changes, because a single real edit anywhere in the file defeats whole-file identity.
- **Recall risk: low.** Patch shapes that would cause a miss:
  - (i) `== None` on an object that is a `reverse_key` or some new None-unsafe `__eq__` class. The guard covers this, as long as the build index finds the class.
  - (ii) A mixin-enum attribute that receives raw strings through `setattr` or `**kwargs` loops that the assignment scan does not see. Mitigation: treat any `setattr(` in the defining class's module as disqualifying.
  - (iii) Tests that read source text. `tests/test_typing.py` reads typing files, so keep `sqlglot/typing/` excluded, as the docstring rule already does (`selector.py:460`).
  - (iv) The canonicalisation must apply only to whole-file identity. Never use it to drop lines from a patch that has other real edits. The rule as given doesn't.

### P2: A value-only edit to a class-attribute literal → coverage of the *value-reader lines*
Trigger: an edit to a class-body assignment `NAME = {k: v, ...}` (or a tuple or list literal) where the old and new values have the same keys or length and only literal values change. Such an edit cannot break import (it evaluates only literals), so the module and family broadcast, the `all_dialect_tests` and the fixture-dialect sets are not needed.

The tests that can see the change are those that execute a line that dereferences a *value* of `NAME`. A value-reader line is one of:
- `X[...]`, `X.get(...)`, `X.items()`, `X.values()`, `X.pop(...)`;
- iteration over `.items()`/`.values()`;
- `{**X}`, `dict(X)` or `list(X)`, which count as readers of everything.

Here `X` is an attribute named `NAME`, or a pass-through alias of it:
- a keyword argument `name=self.NAME` → `self.name`;
- a local `a = self.NAME`;
- a `return X`, which counts as unknown and triggers the fallback.

Key-only uses (`in`, `not in`, `bool()`, `if X`, `len`) are excluded. If any value-reader sits in an import-time function (`index["import_fns"]`), or if alias tracking hits something unknown, fall back to today's rule. Reader lines that coverage never records add no tests: unexecuted code fails no test, and any path the new value could open starts at a covered reader.
- **Sample saving on 4d63:** 27.5 s down to about 5-6 s (23 tests, 4.18 s, all fast; k=1 gives about 6.2 s, k=2 maybe less). That is **about 22 s**. Other sample commits: only literal-table edits would match, and I found none among my three. Another agent should confirm against 535z, gbav and 2qn5, which are class-body edits but probably not value-only.
- **Recall risk: medium-low.** Patch shapes that would cause a miss:
  - (i) Values read through `getattr(self, name)` or `vars()`/`__dict__` loops. Mitigation: index every `getattr(` with a non-constant name in `tokens.py`, `parser.py`, `generator.py` and the dialect metaclasses, and fall back if the class can be reached that way. I have not audited this exhaustively.
  - (ii) Values copied into a derived attribute at class creation, e.g. `_Dialect.__new__` building `ESCAPED_SEQUENCES`. This is the import-time-reader fallback.
  - (iii) A value change that alters the type or arity (int → tuple). Require the same AST node type for each value.
  - (iv) Coverage blind spots for process-pool tests. This is already patched for `test_optimizer` and the TPC tests. For `tokenizer_core` the reader lines run in-process.
  - (v) Edits that also change keys. Treat these as non-value-only and use the current rule.

### P3 (side finding, belongs to the fixture owner): fixture stems are matched as substrings
`selector.py:343` and `:634` use `stem in s`. The dialect `"snowflake"` → `optimizer.sql` → stem `"optimizer"` therefore matches `'/optimizer/tpc-ds/'` and selects tpcds, tpch and about 228 executor tests. A token-boundary match (`s == stem` or `re.search(r'(^|/)stem(\.sql)?$', s)`) cut `optimizer` from 231 tests to 3.
- **Warning:** the same probe cut `qualify_columns` from 167 to 1 test, including `test_annotate_*`. Those strings probably name functions rather than fixtures, but this is **unverified**. Do not ship it without checking how `tests/helpers.py` and `test_optimizer.check_file` build fixture paths.
- **Risk: medium** until that is verified. Savings overlap with P2 on 4d63 and would apply to every Snowflake/BigQuery/etc. class-body edit on hidden commits, potentially 25+ s each.

## (c) Code changes

**`tools/build_index.py`: add three keys to the returned dict.**
```python
# none-unsafe __eq__: classes whose __eq__ touches `other.<attr>` without an isinstance/type/is guard
none_unsafe_eq, pure_enums, mixin_enums, attr_assigns, value_readers = set(), set(), set(), {}, {}
for rel, tree in parsed_sqlglot_modules:              # reuse the existing module loop
    for c in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        bases = {getattr(b, "id", getattr(b, "attr", "")) for b in c.bases}
        eq = next((f for f in c.body if isinstance(f, ast.FunctionDef) and f.name == "__eq__"), None)
        if eq is not None:
            src = ast.unparse(eq)
            if re.search(r"\bother\.\w", src) and not re.search(r"isinstance\(other|type\(other\)|is other", src):
                none_unsafe_eq.add(c.name)
        if bases & {"Enum", "AutoName", "IntEnum"} and eq is None:
            (mixin_enums if bases & {"str", "int", "IntEnum"} else pure_enums).add(c.name)
    for n in ast.walk(tree):                           # attr -> RHS shapes
        if isinstance(n, ast.Assign):
            for t in n.targets:
                a = t.attr if isinstance(t, ast.Attribute) else t.id if isinstance(t, ast.Name) else None
                if a:
                    attr_assigns.setdefault(a, []).append(ast.unparse(n.value))
def enum_typed(a, E, seen=()):
    rhs = attr_assigns.get(a, [])
    return bool(rhs) and all(r.startswith(f"{E}.") or r.startswith(f"{E}(") or
        (re.fullmatch(r"(self|cls)\.(\w+)", r) and r.split(".")[1] not in seen
         and enum_typed(r.split(".")[1], E, seen + (a,))) for r in rhs)
test_assigned = {m.group(1) for p in (root/"tests").rglob("*.py")
                 for m in re.finditer(r"\.(\w+)\s*=[^=]", p.read_text("utf-8", "replace"))}
enum_attrs = {a: E for E in mixin_enums for a in attr_assigns if a not in test_assigned and enum_typed(a, E)}
index.update(none_unsafe_eq=sorted(none_unsafe_eq), pure_enums=sorted(pure_enums), enum_attrs=enum_attrs)
```
For P2, also emit `value_readers[NAME] = [[rel, line, is_import_time], ...]` following the reader and alias rules above (a kwarg `name=self.NAME` adds readers of `self.name`), plus `value_readers_unknown = [NAME, ...]` for names that escape (return, getattr, `**`).

**`selector.py`: P1, inside `python_change` (around `selector.py:457`), before `same = ...`.**
```python
class _EqCanon(ast.NodeTransformer):
    def __init__(self, idx, module_text):
        self.pure = set(idx.get("pure_enums", ())); self.attrs = idx.get("enum_attrs", {})
        self.none_ok = not any(re.search(rf"\b{c}\b", module_text) for c in idx.get("none_unsafe_eq", ["reverse_key"]))
    def _ok(self, a, b):
        if isinstance(b, ast.Constant) and b.value is None:
            return self.none_ok
        if isinstance(b, ast.Attribute) and isinstance(b.value, ast.Name):
            E = b.value.id
            if E in self.pure: return True
            return isinstance(a, ast.Attribute) and self.attrs.get(a.attr) == E
        return False
    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) == 1 and isinstance(node.ops[0], (ast.Eq, ast.NotEq)):
            l, r = node.left, node.comparators[0]
            if self._ok(l, r) or self._ok(r, l):
                node.ops = [ast.Is() if isinstance(node.ops[0], ast.Eq) else ast.IsNot()]
        return node
```
Then change the identity check:
```python
same = ast.dump(old_tree) == ast.dump(new_tree)
how = "ast-identical"
if not same and rel.startswith("sqlglot/") and not rel.startswith("sqlglot/typing/"):
    c = _EqCanon(self.index, old)                       # module text: the pre-patch source
    same = ast.dump(c.visit(ast.parse(old))) == ast.dump(c.visit(ast.parse(new)))
    how = "eq-equivalent"
if not same and not rel.startswith("sqlglot/typing/"):
    ...existing docstring step...
```
The docstring step must reuse the original trees, so canonicalise copies as shown.

**`selector.py`: P2, in `sqlglot_change`, before the per-line loop (`selector.py:499`).**
```python
vo = value_only_class_attr(old_tree, new_tree)        # -> NAME or None: one class-body Assign changed,
if vo and vo not in self.index.get("value_readers_unknown", ()):   # same keys/len, same node types
    readers = self.index.get("value_readers", {}).get(vo)
    if readers is not None and not any(r[2] for r in readers):
        hit = set()
        for r, ln, _ in readers:
            hit |= self.cov(r, [ln])
        return hit, "sqlglot:value-only"               # may be empty when no reader runs
```
`value_only_class_attr` walks the two ASTs and checks the following. If any check fails, it returns None:
- exactly one top-level `ClassDef` statement differs, and it is an `Assign` to the same single Name;
- both values are a `Dict`, `Tuple` or `List` with identical keys (constants) and length;
- every differing leaf is `ast.Constant` → `ast.Constant` of the same Python type, or a Tuple of such.

## (d) Ranked recommendations

| # | change | sample saving | risk |
|---|---|---|---|
| 1 | P1, None-comparison canonicalisation (guard: none-unsafe `__eq__` set) | 24.8 s (66ks) | low |
| 2 | P1, enum `is`/`==` canonicalisation (pure enums; mixin only when enum-typed attr) | 25.5 s (2haz) | low (mixin guard needed: `Dialects` receives raw strs) |
| 3 | P2, value-only class-literal edits → coverage of value readers | about 22 s (4d63) | medium-low (getattr/derived-attr escapes need the fallback) |
| 4 | P3, token-boundary fixture-stem matching (verify the loaders first) | overlaps #3 here, larger on hidden dialect edits | medium until verified |

None of these rules is a patch → tests table. Each one derives from operator semantics or data flow in the pinned source, and the sample answers were used only to sanity-check them.
