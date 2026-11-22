# 09: Equivalent and unobservable edits, detected statically (stdlib `ast` + the coverage map)

## 0. What the sample edits look like (this decides which rules pay)

Each code commit in the sample is a **single-token mutation**: `and`/`or`, `is`/`==`, `<`/`<=`, `==`/`!=`,
a constant tweak, `True`/`False`, a dropped `not`, or an index shift. Nobody renames locals, reorders
dicts or converts strings to f-strings. Of the 12 commits that fail nothing, 3 are
**equivalent mutants or unobservable ones that static analysis can prove**:

| commit | edit (file:line) | why it cannot change any outcome | today |
|---|---|---|---|
| 2haz | `dialect.py:1280` `normalization_strategy is NormalizationStrategy.UPPERCASE` -> `==` | every value ever stored in `.normalization_strategy` is an enum member (`dialect.py:1171,1173`; all `NORMALIZATION_STRATEGY =` assignments are `NormalizationStrategy.X`). Members are singletons with unique values (`AutoName`, `helper.py:50`), so `==` and `is` agree | 25.5 s (tpcds) |
| 66ks | `executor/context.py:53` `self._table is None` -> `== None` | `_table: Table \| None` (`context.py:33`). `executor/table.py:10 class Table` has no `__eq__` and no subclasses, so `==` falls back to identity | 24.8 s (tpcds) |
| 8y7j | `generators/clickhouse.py:55` `if scale == ...MICROS` -> `!=` | line 55 is **absent** from `maps/impact.json.gz` even though the function ran (lines 51-54 return first). The mutated statement never executed in any test | 2.0 s |

The rest are **real semantic changes the suite simply does not catch** (2qn5 `<`->`<=` at a version
boundary, 4d63 base 16->17, fryt `==`->`!=`, gbav/ktz3 `True`/`False`, 5y72 `copy=False`->`True`). No sound
static rule proves those inert, except 5y72 (see R4). Do not try to get the others with the "inert" rules
below; any rule that would accept fryt or gbav would also accept real breakages.

Site counts in `sqlglot/` (ast scan of the pinned source): 394 `is` + 288 `is not` (483 against `None`,
about 30 against enum members), 815 `==`, 176 `!=`, 231 `copy=False`. The hidden set is about 1.5x the
sample, so on the sample rate expect about 3 more `is`<->`==` swaps. Function-body statements that never ran
in a function that did run: **1801 of 27233 (6.6%)**, plus 403 statements in 203 functions that were
defined but never called. So roughly 1 in 13 hidden mutations of function bodies lands on dead code.

Estimated sample saving: **52.3 s** (2haz 25.5 + 66ks 24.8 + 8y7j 2.0), plus 2.5 s from R4. That takes
217.7 s to about 165 s with no new misses: no failing sample commit triggers any of the rules
(checked: every failing commit's changed line has at least one test in the map, and none is an `is`/`==` swap).

## 1. Rules, ranked by value

### R1. Identity <-> equality swap against a singleton (2haz, 66ks)
**Algorithm.** Walk the old and new trees in parallel (`ast.walk` order is deterministic for trees of the
same shape). Accept only if exactly one node pair differs, both are `Compare` with identical operand dumps,
and the op pairs differ only by `Is<->Eq` or `IsNot<->NotEq`. Let `R` be the operand on the far side of the
swapped op and `L` the near side. The swap is equivalent iff `L == R` can be true, or can raise, only when
`L is R`:
* `R` is `None`: safe when `L`'s possible types define no **unsafe** `__eq__`/`__ne__`. An `__eq__` is
  unsafe if it reads `other.<attr>` or `other[...]` outside an `isinstance(other, ...)` or
  `type(...) is type(other)` guard. In the repo: `_Dialect.__eq__` (`dialect.py:217`), `Dialect.__eq__`
  (`:1180`) and `Expression.__eq__/__ne__` (`expressions/core.py:837-841`) are safe.
  `executor/env.py:64 reverse_key.__eq__` (`other.obj == self.obj`) is unsafe, and it escapes through
  `ordered()` (`env.py:190`). So the rule must know `L`'s type: (a) take the annotation of `L`
  (parameter annotation, local `AnnAssign`, or `self.x: T` anywhere in the class); (b) require every class
  named there, and every in-repo subclass of it (`index["subs"]`), to have no unsafe `__eq__`. Builtins are
  fine (their `__eq__` returns NotImplemented against `None`). `Any`, `object` or a missing annotation means
  refuse.
* `R` is `E.M`, a member of a **pure** Enum (no `str`/`int` mixin: `ErrorLevel`, `DType`, `ScopeType`,
  `TrieResult`, `PropertiesLocation`, `IsPredicatePrecedence`): the same `L`-type test as for `None`.
* `R` is a member of a **mixin** enum (`NormalizationStrategy`, `UsingColumnOrder`, `AliasPrecedence`,
  `Dialects` are `str`; `TokenType` is `IntEnum`, `tokenizer_core.py:15`): `"UPPERCASE" == member` is
  true, so you also need **provenance**. `L` is `<expr>.attr`, and every assignment to an attribute named
  `attr` (any `X.attr = v`, any class-body `attr = v`, and any `attr=` keyword) in `sqlglot/` and `tests/` has
  `v` that is `E.Member`, `E(...)`, `None`, or `self.ATTR` where `ATTR`'s assignments pass the same test.
  The `dialect="…, normalization_strategy=lowercase"` strings in the tests go through `dialect.py:1173`
  (`NormalizationStrategy(...)`), so they pass.
* Anything else (`is True`, `is x` against an Expression, which `==` compares **structurally**, or `is` a
  class whose metaclass is `_Dialect`): refuse.

**CPU.** The parallel walk is O(nodes) on trees `python_change` has already parsed, about 2-5 ms for
`dialect.py`. The `eq_safety`, `enum_kind` and `attr_members` tables are built in `build_index.py` (one
ast pass over sqlglot and tests, under 1 s at build time), so the selector does dictionary lookups only.

**False-inert risk: low.** Shapes that would miss: a value of an unannotated or lying type, such as a
`Table | None` slot that really holds a test double with a custom `__eq__` (refused when the annotation is
`Any` or absent, so only a *wrong* annotation can bite); a mixin-enum attribute set through `setattr` or
`__dict__` (add a check: refuse if `setattr(` or `__dict__[` appears with the attribute name as a string
literal anywhere); a third-party `__eq__` (sqlglot has no runtime dependencies).

### R2. Mutation on a statement that never ran (8y7j)
**Algorithm.** For each changed old line inside a function: find the innermost statement containing it.
For a simple statement the span is `lineno..end_lineno`; for a compound header (`if`/`elif`/`while`/`for`/
`with`) it is `lineno..body[0].lineno-1`. Call the statement dead if **no** line of that span is in
`files[rel]["lines"]` while the function's `def` line is (so the file was measured). Absent means executed
by nothing, not even at import: `build_map.py:100-110` stores a key for every executed line, with an empty
set for import-only lines, and `:147-157` pre-fills all of `executor/`, `planner.py` and
`generators/python.py`, so dead never fires there. Extra guards:
* only same-line-count replacements (`same_indent_replacements`); headers, decorators and default
  arguments run at definition time, so they are always present and the rule cannot fire on them;
* the **scope signature** is unchanged. Use `symtable` on the old and new source and compare, for every
  scope, the type, name, and `(name, is_local, is_global, is_free, is_parameter)` of each symbol, plus
  the yield/await flags. Without this, a dead line `x = 1` -> `y = 1` makes `y` local and raises
  `UnboundLocalError` in a line that does run, and a dead `return v` -> `yield v` turns the function into a
  generator.

**CPU.** One table lookup per changed line, plus two `symtable` builds (about 15-30 ms on the largest
file, done only when some line is dead).

**False-inert risk: low to medium. It depends entirely on how complete the map is.** Shapes that would miss:
(1) code that runs only in an unmeasured subprocess. `test_dialect.py:237` `test_lazy_load` runs
`python -c "import sqlglot…"`, but that is import-time code, which in-process tests also execute, so
those lines are present. The TPC process pools are re-run with threads (`.coverage.opt`, `.coverage.exec`,
`build_map.py:73-79`). (2) A test skipped in the map run but run during scoring: the map run had
`1 skipped` (`work/coverage-run.log`). The candidates are the `sqlite_version_info` and
`sys.version_info` `skipIf`s, which are environment-dependent, so lines only those tests reach would look dead.
Mitigation: list each skipped test's id in `build_map` and treat as live (not dead) the functions whose
names appear in that test's strings. (3) The scorer imports a mypyc-compiled sqlglot: no change in
behaviour, because compiled code is not swapped by a source patch.

Note an **existing** hole of the same kind: when a file is entirely missing from the map, `sqlglot_change`
returns an empty set through the fn branch (`selector.py:521-533`, where `hit` stays empty). R2 makes the
measured-file condition explicit; the unmeasured-file case should go to `import_time_change` instead of `[]`.

### R3. Normalising refactors: constant folding, f-strings, annotations, `TYPE_CHECKING`
Do this inside `python_change`'s AST-identical test with a canonicalising `NodeTransformer`, applied to
both trees after `strip_docstrings`:
* **Constant folding**: fold `BinOp`/`UnaryOp` whose operands are numeric or str `Constant`s. Compare by
  `(type, repr)`, so `True` vs `1`, `0.0` vs `-0.0` and `1` vs `1.0` stay distinct. Cap the size: a pow
  exponent of 64 at most, and folded strings of 4096 characters at most. `0xFF` vs `255` and `"a" "b"`
  vs `"ab"` are already identical in `ast.dump`, so today's `ast-identical` covers them.
* **f-strings**: a `JoinedStr` with no `FormattedValue` becomes a `Constant`. Do **not** treat `"%s" % x`
  and `f"{x}"` as equal: `str()` and `format()` differ for str-mixin enums and other types with a custom
  `__format__`.
* **Annotations**: only in modules with `from __future__ import annotations`. The modules without it are
  `trie.py`, `executor/env.py`, `executor/python.py`, `dialects/{singlestore,solr,druid}.py` and the package
  `__init__`s. In those modules drop `arg.annotation` and `FunctionDef.returns`, and set a function-level
  `AnnAssign.annotation` to `Constant(None)`. Keep the target, because a bare `x: int` still makes `x` local.
  **Never** touch a class-body `AnnAssign`: dataclasses (`lineage.py:23`, `diff.py:23-53`) and
  `t.NamedTuple` (`optimizer/pushdown_projections.py:47`) read those annotations. Nothing in sqlglot or
  tests reads `__annotations__` or `get_type_hints`. The one reflection call, `executor/env.py:101`
  `inspect.signature`, uses parameter names only.
* **`if TYPE_CHECKING:` / `if t.TYPE_CHECKING:` / `if typing.TYPE_CHECKING:`**: replace the body with
  `Pass` and keep `orelse`, which does run. 63 sites.
* Renaming locals and reordering dicts or sets: **don't**. Dict order is observable (iteration over
  `FUNCTIONS`, `TRANSFORMS` and `KEYWORDS`, and the last duplicate key wins). Set order is seed- and
  insertion-dependent. Alpha-renaming needs bail-outs for `locals()`, `eval`, `exec`, closures, keyword
  parameters and `**kwargs` forwarding. No such edit appears in a mutation-style set, so the code would
  carry risk for no saving.

**CPU.** One extra transformer pass and `ast.dump`, about 5-20 ms per file, and only when the plain dump
differs. **False-inert risk: very low** with the restrictions above.

### R4 (optional). `copy=` flag flip on a freshly built argument (5y72)
`exp.alias_(exp.table_(name), …, copy=False->True)`: `alias_` uses `copy` only in
`maybe_parse(expression, copy=copy)` (`core.py:3090`, which calls `maybe_copy`, `core.py:2641`), and
`builders.py:530 table_` returns a new `Table(...)` no one else holds. A deep copy of an unshared fresh tree
cannot be observed. Rule: the only change is the `copy` keyword constant, the callee's `copy` parameter
reaches only `maybe_copy`/`maybe_parse`/`.copy()` (check in the index per function name), and the copied
argument is a direct call to an Expression class or to a builder whose every `return` is such a constructor
call. **Risk: medium-low** (an identity-dependent builder, or one returning a cached node). Saves about
2.5 s on the sample.

### R5. Error message, `__repr__` and `__str__` string edits not asserted anywhere
**Algorithm.** The parallel walk finds only `Constant(str)` pairs differing. Every one must sit inside a
`Raise` exception expression, a `self.raise_error(...)`/`logger.*`/`warnings.warn` argument, or a
`__repr__`/`__str__`/`__format__` body. Build the corpus as the text of every file under `tests/`
(12 MB, fixtures included; read lazily only when this rule is tried, about 30 ms). Split the old and new
literals on every non-`[A-Za-z0-9 _]` character, so escapes, quotes and regex metacharacters never need
matching, keep runs of 6 or more characters, and refuse if any run occurs in the corpus or **elsewhere** in
`sqlglot/` (control flow such as `"…" in str(e)`). Tests compare whole error dicts
(`test_transpile.py:1171`) and use 172 `assertRaises*`. All of those carry the literal text, so the grep
sees them. **Risk: medium.** It misses a test that rebuilds the message from pieces shorter than 6
characters, and a repr compared through a fixture that is generated rather than stored. No sample commit
hits this rule, so it saves 0 s on the sample. Implement it last.

## 2. Code

### 2a. `tools/build_index.py`: new index keys (add to the dict written at the end)
```python
def eq_tables(files):  # files: {rel: ast.Module} for sqlglot/ and tests/
    eq, enums, members_ok, ann = {}, {}, {}, {}
    def unsafe_eq(fn):
        arg = fn.args.args[1].arg if len(fn.args.args) > 1 else None
        guarded = any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "isinstance"
                      for n in ast.walk(fn)) or "type(other)" in ast.unparse(fn)
        touches = any(isinstance(n, (ast.Attribute, ast.Subscript)) and isinstance(n.value, ast.Name)
                      and n.value.id == arg for n in ast.walk(fn))
        return touches and not guarded
    assigns = {}
    for rel, tree in files.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for it in node.body:
                    if isinstance(it, ast.FunctionDef) and it.name in ("__eq__", "__ne__"):
                        eq[node.name] = eq.get(node.name, False) or unsafe_eq(it)
                    if isinstance(it, ast.Assign):
                        for tg in it.targets:
                            if isinstance(tg, ast.Name): assigns.setdefault(tg.id, []).append(it.value)
                bases = {ast.unparse(b).split(".")[-1] for b in node.bases}
                if bases & {"Enum", "AutoName", "IntEnum", "StrEnum"} or any(b in enums for b in bases):
                    mixin = bool(bases & {"str", "int", "IntEnum", "StrEnum"}) or \
                            any(enums.get(b) == "mixin" for b in bases)
                    enums[node.name] = "mixin" if mixin else "pure"
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                for tg in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                    if isinstance(tg, ast.Attribute) and node.value is not None:
                        assigns.setdefault(tg.attr, []).append(node.value)
                    if isinstance(node, ast.AnnAssign) and isinstance(tg, ast.Attribute):
                        ann[f"{rel}:{tg.attr}"] = ast.unparse(node.annotation)
            elif isinstance(node, ast.keyword) and node.arg:
                assigns.setdefault(node.arg, []).append(node.value)
            elif isinstance(node, ast.Call) and getattr(node.func, "id", "") == "setattr":
                members_ok[ast.unparse(node.args[1]).strip("'\"") if len(node.args) > 1 else "*"] = False
    def member_expr(v, seen):
        if isinstance(v, ast.Constant) and v.value is None: return True
        s = ast.unparse(v.func if isinstance(v, ast.Call) else v)
        head = s.split(".")[-2] if isinstance(v, ast.Attribute) else s.split(".")[-1]
        if head in enums: return True
        if isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name) and v.value.id in ("self", "cls"):
            return ok(v.attr, seen)
        return False
    def ok(attr, seen):
        if attr in seen: return True
        return members_ok.get(attr, True) and bool(assigns.get(attr)) and \
               all(member_expr(v, seen | {attr}) for v in assigns[attr])
    return {"eq_unsafe": {k: v for k, v in eq.items()}, "enum_kind": enums,
            "attr_members": {a: ok(a, frozenset()) for a in assigns if a.isidentifier()},
            "attr_ann": ann}
# index.update(eq_tables({rel: tree for rel, tree in parsed_sqlglot_and_tests.items()}))
```

### 2b. `selector.py`: equivalence check, called from `python_change`
Insert just before `if rel.startswith("tests/"): return self.test_file_change(...)`:
```python
        if old_tree is not None and new_tree is not None and rel.startswith("sqlglot/") and bytes_inert(f):
            why = self.equivalent_edit(old_tree, new_tree, rel)
            if why:
                return set(), why
```
```python
    def equivalent_edit(self, old_tree, new_tree, rel):
        a, b = canon(copy.deepcopy(old_tree), old_tree), canon(copy.deepcopy(new_tree), new_tree)
        if ast.dump(a) == ast.dump(b):
            return "canonical-identical"                     # R3
        diffs = [(x, y) for x, y in zip(ast.walk(old_tree), ast.walk(new_tree))
                 if type(x) is not type(y) or ast.dump(x, include_attributes=False) !=
                 ast.dump(y, include_attributes=False)]
        # keep only the innermost differing pair (ancestors differ too)
        inner = [p for p in diffs if not any(q is not p and q[0] in ast.walk(p[0]) for q in diffs)]
        if len(inner) != 1 or not isinstance(inner[0][0], ast.cmpop):
            return None
        op_old, op_new = inner[0]
        swap = {(ast.Is, ast.Eq), (ast.Eq, ast.Is), (ast.IsNot, ast.NotEq), (ast.NotEq, ast.IsNot)}
        if (type(op_old), type(op_new)) not in swap:
            return None
        cmp_ = next(n for n in ast.walk(old_tree) if isinstance(n, ast.Compare) and op_old in n.ops)
        i = cmp_.ops.index(op_old)
        left, right = ([cmp_.left] + cmp_.comparators)[i], cmp_.comparators[i]
        ix = self.index
        if isinstance(right, ast.Constant) and right.value is None:
            return "is-none-swap" if self.type_eq_safe(old_tree, left, rel) else None
        if isinstance(right, ast.Attribute) and isinstance(right.value, (ast.Name, ast.Attribute)):
            enum = ast.unparse(right.value).split(".")[-1]
            kind = ix.get("enum_kind", {}).get(enum)
            if kind == "pure" and self.type_eq_safe(old_tree, left, rel):
                return "is-enum-swap"
            if kind == "mixin" and isinstance(left, ast.Attribute) and \
                    ix.get("attr_members", {}).get(left.attr):
                return "is-enum-member-swap"
        return None

    def type_eq_safe(self, tree, expr, rel) -> bool:
        """Every class expr's annotation names (and their subclasses) has a None/foreign-safe __eq__."""
        ann = None
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name) and expr.value.id == "self":
            ann = self.index.get("attr_ann", {}).get(f"{rel}:{expr.attr}")
        elif isinstance(expr, ast.Name):
            for fn in ast.walk(tree):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                        fn.lineno <= expr.lineno <= fn.end_lineno:
                    for a in fn.args.args + fn.args.kwonlyargs:
                        if a.arg == expr.id and a.annotation is not None:
                            ann = ast.unparse(a.annotation)
        if not ann:
            return False
        names = set(re.findall(r"[A-Za-z_]\w*", ann.strip("'\""))) - {"t", "typing", "exp", "None",
                    "Optional", "Union", "int", "str", "bool", "float", "bytes", "list", "dict",
                    "tuple", "set", "frozenset", "List", "Dict", "Tuple", "Set", "Sequence"}
        if names & {"Any", "object", "T", "E"}:
            return False
        unsafe, subs = self.index.get("eq_unsafe", {}), self.index.get("subs", {})
        todo, seen = list(names), set()
        while todo:
            n = todo.pop()
            if n in seen: continue
            seen.add(n)
            if unsafe.get(n): return False
            todo.extend(subs.get(n, []))
        return True
```
`canon(tree, orig)` is the R3 transformer (its module-level `future` flag is read from `orig.body`):
```python
class _Canon(ast.NodeTransformer):
    def __init__(self, future): self.future, self.depth_cls = future, 0
    def visit_ClassDef(self, n): self.depth_cls += 1; self.generic_visit(n); self.depth_cls -= 1; return n
    def visit_If(self, n):
        self.generic_visit(n)
        if ast.unparse(n.test) in ("TYPE_CHECKING", "t.TYPE_CHECKING", "typing.TYPE_CHECKING"):
            n.body = [ast.Pass()]
        return n
    def visit_FunctionDef(self, n):
        saved, self.depth_cls = self.depth_cls, 0
        self.generic_visit(n); self.depth_cls = saved
        if self.future: n.returns = None
        return n
    visit_AsyncFunctionDef = visit_FunctionDef
    def visit_arg(self, n):
        if self.future: n.annotation = None
        return n
    def visit_AnnAssign(self, n):
        self.generic_visit(n)
        if self.depth_cls == 0: n.annotation = ast.Constant(None)
        return n
    def visit_JoinedStr(self, n):
        if all(isinstance(v, ast.Constant) for v in n.values):
            return ast.Constant("".join(v.value for v in n.values))
        return self.generic_visit(n)
    def visit_BinOp(self, n):
        self.generic_visit(n)
        l, r = n.left, n.right
        if isinstance(l, ast.Constant) and isinstance(r, ast.Constant) and \
                type(l.value) in (int, float, str) and type(r.value) in (int, float, str):
            if isinstance(n.op, ast.Pow) and (not isinstance(r.value, int) or abs(r.value) > 64): return n
            try: v = eval(compile(ast.Expression(n), "<c>", "eval"), {"__builtins__": {}})
            except Exception: return n
            if isinstance(v, str) and len(v) > 4096: return n
            return ast.Constant(v)
        return n

def canon(tree, orig):
    future = any(isinstance(s, ast.ImportFrom) and s.module == "__future__" and
                 any(a.name == "annotations" for a in s.names) for s in orig.body)
    return ast.fix_missing_locations(_Canon(future).visit(tree))
```
(`ast.dump` without attributes means the Constant's `kind` and positions don't matter. `eval` here runs only
on two literal constants with no builtins, so it does not touch sqlglot code.)

### 2c. `selector.py`: dead-statement rule (R2) in `sqlglot_change`
At the top of `sqlglot_change`, after `prunable = …`:
```python
        old_src, new_src = self.source(f["old"]), apply_patch(self.source(f["old"]), f)
        scopes_same = None  # computed lazily
        table = self.files.get(rel, {}).get("lines", {})
```
First thing inside `for ln in sorted(changed):`, after `fn, cls = self.innermost(rel, ln)`:
```python
            if fn and ln in prunable and str(fn[0]) in table:
                st = innermost_stmt(old_tree, ln)
                if st is not None:
                    hi = st.body[0].lineno - 1 if getattr(st, "body", None) and not isinstance(
                        st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else st.end_lineno
                    if not any(str(x) in table for x in range(st.lineno, hi + 1)):
                        if scopes_same is None:
                            scopes_same = scope_sig(old_src) == scope_sig(new_src)
                        if scopes_same:
                            reasons.append("dead-stmt")
                            continue
```
```python
def innermost_stmt(tree, ln):
    best = None
    for n in ast.walk(tree):
        if isinstance(n, ast.stmt) and not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and n.lineno <= ln <= n.end_lineno:
            hdr_end = n.body[0].lineno - 1 if getattr(n, "body", None) else n.end_lineno
            if ln <= hdr_end or not getattr(n, "body", None):
                if best is None or n.lineno >= best.lineno: best = n
    return best

def scope_sig(src):
    import symtable
    def walk(tb):
        syms = tuple(sorted((s.get_name(), s.is_local(), s.is_global(), s.is_free(), s.is_parameter(),
                             s.is_assigned()) for s in tb.get_symbols()))
        yield (tb.get_type(), tb.get_name(), tb.get_lineno(), syms)
        for c in tb.get_children(): yield from walk(c)
    tree = ast.parse(src)
    flags = sorted((n.lineno, any(isinstance(x, (ast.Yield, ast.YieldFrom)) for x in ast.walk(n)),
                    any(isinstance(x, ast.Await) for x in ast.walk(n)))
                   for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    return list(walk(symtable.symtable(src, "m", "exec"))), flags
```
(The per-function yield flags may count nested defs; that only makes the comparison stricter, never looser.)
An edit is a single same-indent replacement, so the line numbers in the scope signature line up.

## 3. Ranked recommendations
| # | change | sample s saved | hidden-recall risk |
|---|---|---|---|
| 1 | R1 `is`<->`==` against None or an enum member, with type/provenance proof | 50.3 (2haz, 66ks) | low: needs a wrong annotation or `setattr`-set enum attribute |
| 2 | R2 dead statement (absent from map, def present, same scopes) | 2.0 (8y7j); about 6-7% of hidden body mutations | low-medium: skipped-at-map-time tests, unmeasured subprocesses |
| 3 | R3 canonical AST (fold, f-string, future annotations, `TYPE_CHECKING`) | 0 | very low |
| 4 | R4 `copy=` flip on a fresh constructor argument | 2.5 (5y72) | medium-low |
| 5 | R5 message/repr strings absent from tests | 0 | medium: implement last or skip |
| - | do NOT: dict/set reorder, local rename, `<`/`<=` boundary (2qn5), `%`->f-string | - | real behaviour changes |

Also fix the existing unmeasured-file case (a fn edit in a file absent from the map returns `[]` today):
route it to `import_time_change`, falling back to everything. That costs 0 s on the sample and closes a miss shape.
