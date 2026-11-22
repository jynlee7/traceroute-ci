# 02 - Generator TRANSFORMS, `<lower>_sql`, class flags

Scope: `work/sqlglot/sqlglot/generator.py`, `sqlglot/generators/*.py` (36 files), `dialects/dialect.py`.
Nothing was run. Everything below comes from reading source, `selector.py`, `tools/build_index.py`, and read-only
inspection of `maps/impact.json.gz`.

## (a) Findings

### How the generator wires itself (all of it at import time or at first `Generator()` per class)
1. No `__init_subclass__` and no generator metaclass. `Generator` has `__slots__` and a plain class body
   (`generator.py:92`, slots near `:947`).
2. Dispatch table is built lazily, once per class: `_build_dispatch(cls)` (`generator.py:74-89`), cached in
   `_DISPATCH_CACHE` (`:71`), called from `Generator.__init__` (`:1005-1009`).
   - Step 1: `dict(cls.TRANSFORMS)`.
   - Step 2: for every `dir(cls)` attr ending in `_sql`, `EXPR_CLASSES[attr[:-4]]` is added only `if expr_cls not in dispatch`.
   - So **TRANSFORMS beats `<lower>_sql`**, including a `foo_sql` defined on a subclass when `Foo` is in an
     inherited TRANSFORMS (`**Parent.TRANSFORMS`). Adding a method can therefore be a no-op, and deleting a
     TRANSFORMS entry silently re-enables the method or `function_fallback_sql`.
   - Lookup at `generator.py:1202`: `handler = self._dispatch.get(expression.__class__)`, then
     `function_fallback_sql` for `exp.Func`, `property_sql` for `exp.Property`, else `ValueError`.
   - `json_path_part` (`:4100`) and `transforms.preprocess` (`transforms.py:52`) read `self.TRANSFORMS.get(...)` directly at call time.
3. TRANSFORMS is built by copy at class-body time: `**generator.Generator.TRANSFORMS` (bigquery.py:407, drill:66, dremio:65,
   exasol:331, druid:26, oracle:74, presto:321), `**SparkGenerator.TRANSFORMS` (databricks:50),
   `**TrinoGenerator.TRANSFORMS` (athena:123), `**TSQLGenerator.TRANSFORMS` (fabric:75),
   `**MySQLGenerator.TRANSFORMS` (doris:58), plus filtered comprehensions (mysql:152-158 drops ArrayContainsAll/ContainedBy,
   clickhouse:298, singlestore:46, materialize:23, risingwave:14). The copy means one edit of a parent entry reaches children
   through the shared lambda/function object, not through a line in the child file. Dropping or filtering a key in a child
   is the only way a parent entry stops applying.
4. **In-place mutation at dialect creation**: `dialects/dialect.py:351-355` pops `ALL_JSON_PATH_PARTS - SUPPORTED_JSON_PATH_PARTS`
   from `gen_cls.TRANSFORMS` for every generator class that sets `SUPPORTED_JSON_PATH_PARTS` (10 generators: clickhouse:226,
   sqlite:108, tsql:189, postgres:309, snowflake:635, duckdb:1868, trino:51, singlestore:37, bigquery:556, hive:262). Each of
   those defines its own TRANSFORMS, so the pop hits its own dict, but a class-level flag edit there changes the dict contents
   (not just a read). Base `Generator.SUPPORTED_JSON_PATH_PARTS = ALL...copy()` (`generator.py:544`); `JSON_PATH_PART_TRANSFORMS`
   comes from `jsonpath.py:222`. A flag read in a metaclass at import is invisible to call-time coverage.
5. Flags are plain class attributes (`generator.py` ~144 ClassVar-ish constants; dialect overrides e.g. mysql.py:113-117). They are
   read via `self.FLAG` inside generic `*_sql` methods, so the read-site lines are executed by every dialect. Line coverage
   cannot say which dialect's flag value a test exercised.
6. `AFTER_HAVING_MODIFIER_TRANSFORMS` (`generator.py:61, 715-719`, string keys, read at `:3554`) is a second table with the same
   `lambda self, e:` shape; many dialects re-point it (`generator.AFTER_HAVING_MODIFIER_TRANSFORMS`).

### What the map records for these tables
Checked in `maps/impact.json.gz` for `generators/mysql.py` (441 covered lines):
- Plain entries (`exp.ArrayAgg: rename_func("GROUP_CONCAT")`, line 160) map to **4 tests** = the import-time executors
  (mysql 161-164, 166-168, 171-178, 182-191 all 4). They carry no call-time information because the call happens in
  `dialect.py`'s closure, not on that line.
- Single-line lambdas get the union of import + call tests: `exp.Chr: lambda ...` line 165 -> 10; NullSafeEQ line 192 -> 5.
- Multi-line lambda bodies (179-181) get their own lines (180 -> 6).
So only lambda entries carry usable call-time coverage.

### How `selector.py` treats each shape
| Edit | Path in `selector.py` | Result |
|---|---|---|
| Body of a one-line or multi-line lambda in a TRANSFORMS table | `changed_statement_lines` skips it (`:452-455`); `lambda_range` (`:243`) -> `cov(lambda lines)` (`:311-317`) | call-time tests of that lambda, for every dialect that inherited it. Sound for body edits. |
| Non-lambda entry (`rename_func(..)`, function name, `date_add_sql("ADD")`) in a dialect generator | `import_time_change` (`:250`): `dialect_family` file coverage + `name_tests(stems)` + `class_tests(top class)` | whole dialect family. Sound only if fixture-driven tests are caught (see risk 3). |
| Same, in `generator.py` | `dialect_family` = `{generator.py}` (not under DIALECT_DIRS); `file_cov(generator.py)` + `class_tests("Generator")` | ~every test: sound, maximally costly. |
| Class flag (`SUPPORTS_X = True`) | no fn, no lambda -> `import_time_change` | same as non-lambda entry |
| New `def foo_sql` | `new_method_tests` (`:337`): `fns[name]` coverage plus `class_tests(Foo)` | only tests that execute a line that *names* `Foo`, or a `foo_sql` body. |
| Edit of a `foo_sql` body | fn rule with WINDOW=2 | precise |

## (b) Recall risks and cost waste

1. **Added TRANSFORMS entry next to lambda entries (MISS).** `touched_old_lines` (`:81`) returns only the neighbour old lines of an
   insertion. If both neighbours are one-line lambdas (very common: mysql.py:192-193, generator.py base table), the neighbour is
   inside a lambda range, `changed_statement_lines` skips it and `sqlglot_change` takes the `lambda` branch -> `cov(neighbour lambda)`.
   Tests that generate the *new* key (`exp.Bar`) are not those. Same for a lambda-line key swap
   `exp.Foo: lambda ...` -> `exp.Bar: lambda ...` on the same line: the touched line is inside the lambda range, so it is
   treated as a body edit and selects Foo's tests, not Bar's. The `n.lineno..end_lineno` lambda range is line-granular and does
   not tell key text from lambda text.
2. **Adding `foo_sql` for a Func that currently goes through `function_fallback_sql` (MISS).** Generation of `Foo` via the fallback
   executes no line naming `Foo` (parser builders use `sql_names`, not the class name), so `class_tests(Foo)` finds only the
   handful of tests with an explicit `exp.Foo` reference. The same applies to a new non-lambda TRANSFORMS entry for such a Func
   in a dialect with no `foo_sql`: it only gets the family rule, which works only if risk 3 is closed.
3. **Fixture-driven dialects (MISS-prone).** `name_tests` relies on `test_strings` (string constants in the test AST). The dialect
   comes from fixture headers in `tests/fixtures/**.sql` for many tests: `# dialect: mysql` appears 187x, snowflake 910x, bigquery
   524x, duckdb 212x, postgres 122x, spark/databricks ~300x (`grep` over `tests/fixtures`; used by
   `test_optimizer.py::{test_simplify,test_canonicalize,test_optimizer,test_qualify_columns,test_annotate_funcs}`). `build_map.test_strings`
   (`tools/build_map.py:42-66`) never reads `.sql` fixtures, so those tests have no `"mysql"` string. They are picked up
   only if they happen to execute a line in a family file at call time. A `rename_func` entry executes none.
4. **`test_generator_memory` (6.7 s) / flag tables.** It instantiates `dialect.generator()` for every dialect
   (`tests/test_generator.py:19-38`) and asserts on `SELECT_KINDS`-like per-instance memory. No call-time line in
   `generators/*.py` runs for it, so a flag or tuple edit in a dialect generator reaches it only through `name_tests` (strings: none,
   it iterates `Dialects`) or `file_cov(family)` (empty). Under-selection is possible; cost if selected is the 6.7 s floor.
5. **JSON-path flag (`SUPPORTED_JSON_PATH_PARTS`) / `jsonpath.py:222` edits.** Consumed only in the metaclass at import
   (`dialect.py:352`). `import_time_change` takes the family coverage, but the effect is on TRANSFORMS of that generator, hence on
   `test_jsonpath.py` and all `JSONExtract` tests of the family. `jsonpath.py` itself is not in DIALECT_DIRS, so its edit goes
   `file_cov(jsonpath.py)` + `class_tests` - probably fine, but the `generator.py:132` spread is not followed.
6. **Shadowed edits.** Edit in base `Generator.foo_sql` that every dialect shadows by TRANSFORMS, or a `foo_sql` added to a subclass
   whose inherited TRANSFORMS has `Foo`: no behaviour change; the selector still pays (cost waste only).
7. **Base-table non-lambda entry in `generator.py`** (e.g. `exp.ILike: no_ilike_sql`) selects ~everything (`file_cov(generator.py)`), the
   most expensive path (the slow tests, 28.8 + 21.5 s, join). A key edit here only affects tests that generate that class.

## (c) Proposed changes

### C1. Key-level diff of class-level dict tables (selector.py, fixes risks 1, 7)
Add a helper next to `def_names` and call it in `sqlglot_change` before the per-line loop.

```python
def table_entries(tree) -> dict[tuple[str, str], dict[str, ast.AST]]:
    """(class, attr) -> {key source: value node} for every class-level dict literal."""
    out: dict[tuple[str, str], dict[str, ast.AST]] = {}
    if tree is None:
        return out
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        for st in cls.body:
            tgt = st.targets[0] if isinstance(st, ast.Assign) else getattr(st, "target", None)
            val = getattr(st, "value", None)
            if isinstance(tgt, ast.Name) and isinstance(val, ast.Dict):
                ents = {}
                for k, v in zip(val.keys, val.values):
                    ents[ast.unparse(k) if k is not None else "**" + ast.unparse(v)] = v
                out[(cls.name, tgt.id)] = ents
    return out


def table_key_changes(old_tree, new_tree) -> set[str]:
    """Keys added, removed, or whose value is not a same-key lambda-to-lambda edit, in any class table."""
    o, n = table_entries(old_tree), table_entries(new_tree)
    keys: set[str] = set()
    for tab in set(o) | set(n):
        oe, ne = o.get(tab, {}), n.get(tab, {})
        for k in set(oe) | set(ne):
            if k not in oe or k not in ne:
                keys.add(k)
            elif ast.dump(oe[k]) != ast.dump(ne[k]):
                if not (isinstance(oe[k], ast.Lambda) and isinstance(ne[k], ast.Lambda)):
                    keys.add(k)
    return keys
```

In `sqlglot_change`, after `changed = ...`, when `rel.startswith(("sqlglot/generators/", "sqlglot/generator.py", "sqlglot/parsers/"))`:

```python
        keys = table_key_changes(old_tree, new_tree)
        for k in keys:
            ln = ...  # any touched line; use min(changed) if changed else 1
            if k.startswith("**"):                      # spread/filter of a parent table changed
                out |= self.import_time_change(rel, min(changed or {1}))
            else:
                out |= self.import_time_change(rel, min(changed or {1}))
                name = k.rsplit(".", 1)[-1]
                if name in self.index.get("refs", {}):
                    out |= self.class_tests(name)        # every line naming Foo, any file
            reasons.append("table-key")
```
Also, in the same function, a *lambda-branch guard*: if `keys` is non-empty, skip the `lambda_range` shortcut for lines inside the
changed entries (it is correct only for same-key lambda body edits). Concretely replace `lam = ...` with
`lam = None if keys else self.lambda_range(rel, ln)`.

Recall-safe because: a key set change affects exactly tests that generate `Foo` for the edited class family; `import_time_change` is
a superset of today's rule, so the new rule never selects less than the current one.

### C2. Dialect strings from fixtures (tools/build_map.py, closes risk 3)
In `test_strings`, for each test function, find string constants that end in `.sql` (they are fixture names:
`load_sql_fixtures("optimizer/simplify.sql")`, `load_sql_fixture_pairs(...)`, `"identity.sql"`) and add the dialect names from the
`# dialect:` headers of that fixture, plus every comma-split token. Derived from the pinned tree only; no answer tuning.

```python
FIX = ROOT / "tests" / "fixtures"
HDR = re.compile(r"^#\s*dialect:\s*(.+)$", re.M)

def fixture_dialects(consts: set[str]) -> set[str]:
    out = set()
    for c in consts:
        if c.endswith(".sql") and (FIX / c).is_file():
            for m in HDR.finditer((FIX / c).read_text()):
                out |= {d.strip() for d in m.group(1).split(",") if d.strip()}
    return out
# in test_strings.consts(): return base | fixture_dialects(base)
```
(add `import re`). `name_tests` then picks the optimizer fixture tests for any `generators/mysql.py` edit.

### C3. Generator-dispatch map recorded during the coverage run (tools/build_map.py + tools/coverage.sh; fixes risks 2, 4, 5)
This is the one rule that makes "TRANSFORMS key `exp.Foo` -> every generator path that handles Foo for that dialect family" exact.
Add a pytest plugin `tools/gendispatch.py`, loaded by `tools/coverage.sh` with `-p tools.gendispatch` (PYTHONPATH includes `/w`):

```python
import json, os, pytest
from sqlglot import generator as g

_cur = {"id": None}
PAIRS: dict[str, set] = {}       # "GenClass|ExprClass" -> test ids
INST: dict[str, set] = {}        # "GenClass" -> test ids

_orig_init, _orig_sql = g.Generator.__init__, g.Generator.sql

def _init(self, *a, **k):
    INST.setdefault(type(self).__name__, set()).add(_cur["id"])
    _orig_init(self, *a, **k)

def _sql(self, expression, key=None, comment=True):
    if not key and expression is not None and not isinstance(expression, str):
        PAIRS.setdefault(f"{type(self).__name__}|{type(expression).__name__}", set()).add(_cur["id"])
    return _orig_sql(self, expression, key, comment)

g.Generator.__init__, g.Generator.sql = _init, _sql

@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item): _cur["id"] = item.nodeid

def pytest_sessionfinish(session):
    p = os.environ.get("GENMAP_OUT", ".genmap") + f".{os.getpid()}"
    json.dump({"pairs": {k: sorted(v) for k, v in PAIRS.items()}, "inst": {k: sorted(v) for k, v in INST.items()}}, open(p, "w"))
```
(`Generator.__slots__` blocks instance attrs but class-level patching is fine; `sql` signature must be copied from `generator.py:~1180`;
no new attrs are set on instances.) `build_map.py` merges the `.genmap.*` files, interns the id lists with its `intern()` into the same
`sets` table, and writes `payload["dispatch"] = {"pairs": {"Gen|Expr": set_id}, "inst": {"Gen": set_id}}`. Overhead is offline only.
Size: restrict to pairs whose Expr is a TRANSFORMS key or has a `*_sql` method if the file grows past a few MB.

Selector use (new method `generator_dispatch_tests(rel, line, keys, flag_names)`):

```python
def gen_closure(self, cls_name: str) -> set[str]:
    """Generator class and every subclass (index["subs"])."""
    subs, seen, todo = self.index.get("subs", {}), set(), [cls_name]
    while todo:
        n = todo.pop()
        if n not in seen:
            seen.add(n); todo.extend(subs.get(n, []))
    return seen

def dispatch_tests(self, cls_name: str, expr_names: set[str] | None) -> set[int] | None:
    d = self.index_map.get("dispatch")          # loaded from impact.json.gz
    if not d:
        return None                              # fall back to import_time_change
    out: set[int] = set()
    for g in self.gen_closure(cls_name):
        if expr_names is None:                   # class flag / unknown key: everything that built this generator
            sid = d["inst"].get(g)
            if sid is not None: out |= self.test_set(sid)
        else:
            for e in expr_names:
                sid = d["pairs"].get(f"{g}|{e}")
                if sid is not None: out |= self.test_set(sid)
    return out
```
Rules built on it (always unioned with the existing rules for anything not understood; never intersected away):
- TRANSFORMS key `exp.Foo` added/removed/changed in generator class G: `dispatch_tests(G, {"Foo"})`. For the base table `Generator`,
  the closure is every generator: `pairs[*|Foo]`, a few hundred tests instead of `file_cov(generator.py)`. For a class whose own table
  spreads a parent (`**Parent.TRANSFORMS`) the parent's closure already includes it.
- Added/renamed `foo_sql` in G (replaces the `class_tests` branch of `new_method_tests` for generator files): `dispatch_tests(G, {"Foo"})` - also
  catches fallback-path tests (risk 2).
- Class flag/tuple edit in generator class G (non-lambda, not in a function): `dispatch_tests(G, None)`, optionally intersected with
  `cov(read-site lines)` only if the flag name's only readers are `self.FLAG` in generator.py (guard: if any reader is outside
  `sqlglot/generator.py` or `generators/`, do not intersect). This picks up `test_generator_memory` (instantiates all) for any flag edit.
  Keep the family fallback (`import_time_change`) unioned for the first version; drop it only after the recall check below.
- `SUPPORTED_JSON_PATH_PARTS`, `AFTER_HAVING_MODIFIER_TRANSFORMS`, `jsonpath.py` table edits: `dispatch_tests(G, {all JSONPath* exprs})` unioned with
  `test_jsonpath.py`; if parsing fails go to `import_time_change`.

Name matching: generator class name for a file is `top_class(rel, line)` (index["toplevel"], already built). Nested-dialect generators
(athena `_HiveGenerator`, `AthenaTrinoGenerator`) are covered by `subs` because the index records every `bases` name.

### C4. Cheap alternative if C3 is rejected (no new coverage run)
Add to `build_index.py` a table `sql_names`: for every `exp.Func` subclass (static `class Foo(Expression, Func)` plus `_sql_names`
literals), the upper-cased SQL names. In `selector.py`, for a changed key `Foo` in a dialect family F, select tests that
(1) satisfy `name_tests(F stems)` and (2) have any string whose upper-case contains one of Foo's sql names. This is weaker than C3
because it misses fixture-driven tests (needs C2) and tests that build the function through the AST API (`exp.Foo(...)`, already caught by
`class_tests`).

## (d) Ranked recommendations

1. **C1 (table key diff).** Closes the only outright miss I can show in the current rules (insertion/key swap beside lambda lines). About 40
   lines, no new data, strictly widens selection. Cost: one more family-wide selection on commits that add/remove entries (for the 30 sample
   patches: negligible if none do, the hidden set may).
2. **C2 (fixture dialect strings).** Closes the fixture-header blind spot behind every `name_tests` rule (all dialect edits, not just generators). Tiny code,
   offline build only. Also cuts nothing; it only adds recall.
3. **C3 (dispatch/instance map).** Highest value: it turns the family rules into exact "tests that dispatched Foo in this class family", catches the
   fallback-path and `test_generator_memory` cases, and likely reduces cost a lot on generator-wide edits (`file_cov(generator.py)` -> `pairs[*|Foo]`). Costs one extra
   offline coverage run plus plugin; map size must be watched. Ship it unioned with today's rule first; measure recall vs the full-suite mutation corpus in
   `plan.md` step 1 before dropping the family fallback.
4. **Flag edits: pair `dispatch_tests(G, None)` with the read-site lines** once C3 exists. Drops cost on dialect-flag edits; do not drop the family fallback
   for flags read in `dialect.py`/metaclass (e.g. `SUPPORTED_JSON_PATH_PARTS`).
5. **Document shadowing**: nothing to code; treat a new `foo_sql` in a class whose TRANSFORMS contains `Foo` as a no-op only after C3 confirms zero
   `pairs[G|Foo]` dispatches would change (otherwise keep selecting).
6. **Do not** add a rule keyed on edit kinds; every rule above is derived from pinned-commit structure (AST of the patched file against the pinned file, the
   import graph, the coverage/dispatch map), consistent with BRIEF rule 4.

## Summary of the proposed generator rule
"Edit to TRANSFORMS key `exp.Foo` (added, removed, or non-lambda-value change) in generator class G, or an added/renamed `foo_sql` in G, selects
`dispatch_tests(G, {Foo})` = tests that dispatched `Foo` in G or any subclass of G (C3), unioned with `import_time_change` (family coverage + dialect names incl.
fixture dialects (C2) + `class_tests(Foo)`), and a same-key lambda-body edit keeps the lambda line coverage."
