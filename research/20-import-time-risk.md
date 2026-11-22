# 20 - Where the selector must fall back to import-time rules

Method: `ast` over all 184 files in `work/sqlglot/sqlglot` (static, nothing imported or run). A "function statement" is a non-def/class statement inside a def, nested ones included. An "import-time statement" is a class-body or module-level statement outside any def. The selection estimate re-implements `import_time_change` as I read it from `selector.py:199-211` and `build_index.py`: family coverage, `name_tests` and `class_tests`. Coverage is read from `maps/impact.json.gz`. This is an approximation without WINDOW and the other rules. Numbers are averages over statements, so they stand for "a random single-line edit".

## (a) Findings

1. **Weight of import-time code.** There are 26,213 function statements against 5,942 import-time ones, so 18.5% of statements are import-time. By raw lines it is 44.5k in functions, 21.8k at class/module level and 3.0k in class-table lambdas (69.3k code lines in total). A random edit lands at import time about 1 time in 5.4. Function statements are well covered: 2,064 of 26,213 (7.9%) have no coverage.
2. **Expected cost of an import-time edit under the current rules is about 606 tests and 42 s**, against 103 s for the whole suite. Function edits cost far less. Rough weight of import-time edits versus a function edit: ~0.18 x 42 s = 7.7 s of expected cost per random edit, from this branch alone.
3. **Ranking by P(land at import time) x selection cost** (columns: file, import-time statements, share of the file's statements, mean tests selected, mean cost):

| file | imp stmts | imp share | mean tests | mean cost |
|---|---|---|---|---|
| `sqlglot/tokenizer_core.py` | 460 | 47% | 1568 | 102 s |
| `sqlglot/dialects/dialect.py` | 308 | 26% | 1588 | 103 s |
| `sqlglot/parser.py` | 211 | 4% | 1522 | 101 s |
| `sqlglot/expressions/core.py` | 192 | 19% | 1541 | 97 s |
| `sqlglot/tokens.py` | 94 | 78% | 1567 | 102 s |
| `sqlglot/expressions/query.py` | 175 | 39% | 1002 | 66 s |
| `sqlglot/expressions/datatypes.py` | 162 | 81% | 840 | 66 s |
| `sqlglot/generator.py` | 172 | 5% | 1356 | 69 s |
| `sqlglot/_typing.py` | 46 | 100% | 1536 | 93 s (61% fall to "all") |
| `sqlglot/generators/duckdb.py` | 98 | 6% | 591 | 59 s |
| `sqlglot/dialects/presto.py` | 38 | 100% | 301 | 43 s |
| `sqlglot/dialects/trino.py` | 12 | 100% | 198 | 35 s |

   - Files that are 100% import-time with zero function coverage: `expressions/string.py`, `expressions/math.py`, all `dialects/*.py` (declarative, `bigquery.py` 68 stmts, `hive.py` 43, `postgres.py` 43), and the `__init__.py`/`_typing.py` files.
   - The dialect files are cheap (mean 0.1-20 s). The expensive part is the top 5 base files above.
   - `tokenizer_core.py` alone has 7.7% of all import-time statements in the package. 449 of them are `X = auto()` members of `class TokenType(IntEnum)` (`tokenizer_core.py:15-479`). Today every edit there takes `class_tests("TokenType")`, i.e. every line naming `TokenType` (about 2,600 textual uses in `sqlglot/`), so ~1,568 tests and ~102 s. This is the biggest single waste in the import-time branch.
4. **Import-time lines are mis-attributed in the coverage map.** Coverage contexts start per test, so a module imported lazily has its class-body lines credited only to the first test that imported it. Examples from `impact.json.gz` (`ntests` = union of tests over all lines of the file): `dialects/bigquery.py` 4, `dax.py` 3, `hive.py` 4, `postgres.py` 8, `tsql.py` 7. The base modules imported at collection time (before any test context) have **no import-time coverage at all**. Files with zero coverage in the map are `__main__.py`, `_typing.py`, `expressions/__init__.py`, `expressions/math.py`, `expressions/string.py`, `parsers/__init__.py` and `parsers/base.py`. So `file_cov` is not a real "who imports this" signal. The rules rely on `name_tests` and `class_tests` instead, and they fall to `set(self.all)` ("module-level-unmapped", `selector.py:~255`) when those are empty. By my estimate that fallback is hit by 13% of the statements in `expressions/math.py` and 61% of those in `_typing.py`.
5. **Static import graph from the tests** (`ast`, `TYPE_CHECKING` blocks skipped, `__getattr__` lazy edges in `optimizer/__init__.py:48-63` and `dialects/__init__.py:120-125` modelled):
   - `import sqlglot` eagerly pulls 36 of 184 modules. With function-level imports counted (conservative) it is 51. Every test file imports `sqlglot`, so for the base modules reachability narrows nothing (parser, generator, tokens, tokenizer_core, expressions/*, dialect.py, helper, errors, schema, diff, `_typing`).
   - Modules **not** in the base closure, and how many tests reach them. These come from a per-test-file closure with dialect names found as string literals in the test file, so the dialect rows are an upper bound:
     - `anonymize.py`: 1 test file (29 tests, 0.0 s)
     - `executor/*`, `planner.py`: 1 file (`test_executor.py`, 60 tests, 31.6 s)
     - `lineage.py`: 2 files (83 tests)
     - `sqlglot/__main__.py`: **no test file**. `tests/test_docs.py:16` explicitly skips it, and `load_tests` is ignored by pytest anyway. A change there should select nothing (the current rule already gives 13 tests and 0.0 s).
     - dialect-specific modules (`dialects/X`, `parsers/X`, `generators/X`, `typing/X`): median 1132 / 1132 / 1037 / 1417 tests. That is far wider than the current coverage, family and name rules (`dialects/*` means: 0.1-20 s). The reason is that dialects import each other (`mysql` and `tsql` are reachable from all 58 files, 1,620 tests), and "dialect name appears as a string" is common.
   - `serde.py` is reached only through the function-level imports in `expressions/core.py:532,541,896`, so a closure that ignores function-level imports wrongly shows 0 tests. `tests/test_serde.py`, Athena, Hive, Spark tests import it explicitly, and the optimizer process pool uses it through `Expression.__reduce__`.
   - The per-test-file closure holds no "tests that import nothing from sqlglot besides the base", so there is no module in the package that no test imports except `__main__`. There are no dead modules otherwise.
6. **Consequence for the proposed reachability map.** For import-time edits it can only *narrow* edits to the ~12 non-base modules (executor x5, planner, lineage, anonymize, `__main__`, a few optimizer modules when lazily reached). For the rest it is a no-op, because the closure is the whole suite. Used as a union with the existing rules it would hurt (dialect rows above); as an intersection it can only help where the existing rules fall to `set(self.all)`.

## (b) Recall risks and cost waste

Recall risks under the current `selector.py`:
- **R1. Value-only edit to an import-time table that is read lazily, in a module whose own coverage is empty** (e.g. `expressions/math.py` or `string.py`: 0 tests covered). `import_time_change` then relies only on `class_tests(cls)`. That is sound when the table is a class attribute named in `refs`, but an edit to a module-level constant that is read through `exp.<Name>` or a dict lookup by a string key (`_LAZY_ATTRS`, `DIALECT_MODULE_NAMES`, `TOKEN_TYPE...`) has `top_class(...) = None`, so only family/name rules apply. For `dialects/__init__.py` the "package-init" rule covers it. For non-`__init__` module-level dicts keyed by strings, hits can come from the first-importer attribution only, which is order-dependent.
- **R2. Edits to the dialect registry** (`DIALECT_MODULE_NAMES` / `Dialects` enum in `dialects/dialect.py`). `dialect.py` is covered at import by nobody; `top_class("Dialects")` gives `class_tests`, which is wide, so this is safe but expensive (103 s).
- **R3. A new class attribute with a name clash.** Adding `X = ...` to a top-level class whose name is rarely mentioned selects only the tests that cover lines naming that class. A subclass attribute override found only by dynamic access (`getattr(cls, name)`, e.g. generator `TRANSFORMS` keyed by Expression class, parsers `FUNCTIONS` by string) is not in `refs`. The existing `new_method_tests` covers `def`s only, not attributes.
- **R4. Suppose an `__init__.py`-style re-export file I did not list**: `sqlglot/generators/__init__.py`, `parsers/__init__.py`, `typing/__init__.py`, `executor/__init__.py`, `optimizer/__init__.py`. The code takes "package-init" for all `__init__.py` module-level edits, which is sound (all tests). Only wasteful for `executor/__init__.py` (it only matters for the 60 executor tests) - see (c).

Cost waste (the expected cost per import-time edit is 42 s vs. a ~3 s lower bound on most of these):
- **W1.** `tokenizer_core.py` `TokenType` members: ~102 s per edit for what is almost always a rename/add/remove of one enum member.
- **W2.** `tokens.py` (94 import-time statements, 78% of the file; the `Tokenizer` class tables) and `dialect.py` class tables select ~1,570 tests (~102 s) because `class_tests("Tokenizer")` / `("Dialect")` is the whole suite.
- **W3.** `executor/__init__.py` and `planner.py` import-time edits: the "package-init -> all" rule sends 1,620 tests / 103 s where the closure has 60 tests / 31.6 s.
- **W4.** `_typing.py`: 61% of statements fall to "module-level-unmapped -> all" (it has zero coverage). Edits to `TypeVar`/alias statements are runtime-neutral but can not be proved so statically. Leave as is.

## (c) Proposed code changes

### C1. `tools/build_index.py` - add attribute references and the reachability map

Inside `build()`, after `rdeps` is complete and before the `return`, add:

```python
    # (class, attr) -> [file, line, line] of every `Class.attr` use (enum members, class tables)
    attr_refs: dict[str, list] = {}
    for rel, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in class_names:
                attr_refs.setdefault(f"{node.value.id}.{node.attr}", []).append([rel, node.lineno, node.end_lineno or node.lineno])

    # module -> test files whose import closure holds it (conservative: function-level imports count)
    edges: dict[str, set] = {}
    for rel, tree in trees.items():
        edges[rel] = {f for f in (module_file(t, root) for t in _import_targets(rel, tree)) if f and f != rel}
    # optimizer/__init__ resolves submodules lazily (PEP 562): it reaches all of them
    edges["sqlglot/optimizer/__init__.py"] |= {r for r in trees if r.startswith("sqlglot/optimizer/")}
    names = {Path(r).stem for r in trees if r.startswith("sqlglot/dialects/")} - {"__init__", "dialect"}
    reach: dict[str, list] = {}
    for tp in sorted((root / "tests").rglob("*.py")):
        trel = str(tp.relative_to(root))
        try:
            ttext = tp.read_text(encoding="utf-8")
            seeds = {"sqlglot/__init__.py"} | {f for f in (module_file(t, root) for t in _import_targets(trel, ast.parse(ttext))) if f}
        except Exception:
            continue
        for d in names:  # dialects are imported by name: any string literal naming one
            if f'"{d}"' in ttext or f"'{d}'" in ttext:
                seeds |= {f for p in ("dialects", "parsers", "generators", "typing") if (f := f"sqlglot/{p}/{d}.py") in trees}
        seen, todo = set(), list(seeds)
        while todo:
            x = todo.pop()
            if x not in seen:
                seen.add(x)
                todo.extend(edges.get(x, ()))
        for m in seen:
            reach.setdefault(m, []).append(trel)
```

with the import-target extraction of `build()` lifted into a helper (it already exists inline at `build_index.py:~85-98`; move it to `_import_targets(rel, tree)` returning the same `targets` list for every `Import`/`ImportFrom` in `ast.walk(tree)`), and add `"attr_refs": attr_refs, "reach": {k: sorted(v) for k, v in reach.items()}` to the returned dict. Keep a size check (the index is not charged at runtime but `INDEX` load is: reach is ~180 modules x <= 60 test files, a few hundred KB; store file lists, not per-test ids).

Where tests also import `tests/helpers.py`, `tests/dialects/test_dialect.py` etc., `_import_targets` on the test file only sees `sqlglot.*`; helpers import `sqlglot` too, so seeds above are enough.

### C2. `selector.py` - an intersection guard for broad fallbacks

Add a method next to `lambda_range`:

```python
    def importers(self, rel: str) -> set[int] | None:
        """Tests in files whose static import closure holds module `rel`, or None if unknown."""
        files = self.index.get("reach", {}).get(rel)
        if files is None:
            return None
        out: set[int] = set()
        for tf in files:
            out |= self.by_file.get(tf, set())
        return out
```

and in `sqlglot_change`, replace the two broad fallbacks:

```python
                if rel.endswith("__init__.py"):
                    imp = self.importers(rel)
                    if imp is not None and rel not in ("sqlglot/__init__.py", "sqlglot/dialects/__init__.py", "sqlglot/optimizer/__init__.py"):
                        out |= imp          # e.g. executor/__init__.py -> test_executor only
                        reasons.append("package-init-reach")
                        continue
                    return set(self.all), "package-init"
                hit = self.import_time_change(rel, ln)
                if not hit:
                    imp = self.importers(rel)
                    if imp:
                        out |= imp
                        reasons.append("module-reach")
                        continue
                    return set(self.all), "module-level-unmapped"
```

Do **not** intersect `import_time_change`'s result with `importers()` for dialect files: the closure is only a heuristic over string literals (fixtures, `Dialect.classes`, `dialect=` read from variables). Use it as a union floor only for the 12 non-base modules above if wanted, and never narrow below the current rules.

### C3. Enum-member precision (the W1 fix)

In `import_time_change`, before `class_tests`, add an attribute-level path used only when the changed statement is an assignment to a name inside a top-level class (value-only, i.e. the target name appears in both old and new tree):

```python
    def attr_tests(self, cls: str, attr: str) -> set[int] | None:
        refs = self.index.get("attr_refs", {}).get(f"{cls}.{attr}")
        if not refs:
            return None
        out: set[int] = set()
        for rel, a, b in refs:
            out |= self.cov(rel, range(a, b + 1))
            if not out and not self.innermost(rel, a)[0]:   # an import-time reader: take its importers
                out |= self.importers(rel) or set()
        return out
```

Call it from `sqlglot_change` for statements whose target name exists on both sides of the diff (a pure RHS edit). Names that are **added or removed** must keep `class_tests(cls)`: a removed member breaks every module-level reference at import (collection error, everything fails), and an add shifts `auto()` values. Gate this with the AST check that the only differences are inside that class body and that the class bases include `IntEnum`/`Enum` (for `TokenType`). Expected saving: an RHS edit of a `TokenType` member goes from 102 s to a handful of tests.

## (d) Ranked recommendations

1. **(C2, safe, small)** Use `reach` only where the current code returns "all": `package-init` for `executor/__init__.py`, `generators`, `parsers`, `typing` inits, and "module-level-unmapped" for `planner.py`, `executor/*`, `lineage.py`, `anonymize.py`, `serde.py`. Saves ~70 s per such edit (103 s -> 31.6 s), but those files are 6 of 184 and about 1% of statements. Expected gain is small (<0.5 s per commit on average); recall risk is low as long as `sqlglot/__init__.py`, `dialects/__init__.py`, `optimizer/__init__.py` stay on "all".
2. **(C3, medium)** Attribute-level rule for value-only edits of class tables and enum members. Targets W1 and part of W2 (tokenizer_core 460 + tokens 94 + dialect.py 308 statements = 862 of 5,942 import-time statements, 14.5%, each currently 100+ s). Potential saving: 14.5% x 18.5% x ~90 s = ~2.4 s per commit on average if a quarter of them are value-only edits, more if most are. Adds R3-style risk: keep removals/additions on the broad rule, and require the old and new AST to differ only in that class body.
3. **(Do nothing for `__main__`)** The current rule gives 13 tests / 0.0 s; the closure says no test imports it. Leave as is, or set it to `set()` if the compliance rules accept "no test imports it". It is rare and the saving is tiny.
4. **Do not use reachability as a union** or as the main narrowing for dialect files: the closure is 1,000+ tests for most dialects (mysql/tsql: all 1,620), against 0.1-20 s from the current coverage + name rules. It is conservative-by-construction, so it is the correct *safety net* but a poor selector.
5. **Keep the base-file import-time edits broad** (`parser.py`, `generator.py`, `expressions/core.py`, `tokens.py`, `_typing.py`, `helper.py`). Reachability cannot narrow them (all 1,620 tests import them), and a miss costs 300 s, about 3 full-suite runs. If the compliance scorer allows it, only C3 narrows them.
6. **Fix the first-importer attribution at the source** (`tools/build_map.py`/coverage run): run one extra "import everything" pass (all dialect modules imported inside a `dynamic_context`) and record import-time lines as belonging to the importer set from `reach`, or simply add the `reach` test set to `file_cov` for lazily-imported modules. This removes finding 4 (`ntests` 3-8 for modules that 30+ test files import). It is a build-time change, but it costs a full re-run of the coverage tooling, so it is last. Expected impact: the `file_cov` term of `import_time_change` becomes meaningful instead of arbitrary, improving recall for R1-type edits to dialect files.

Evidence scripts (read-only, `ast` only): `/private/tmp/claude-501/-Users-leej741-projects-traceroute-ci/26d6def5-0f73-4c94-988e-3cb43296f6dc/scratchpad/{a,s,g,h2}.py`; outputs `rows.json`, `sel.json`, `reach.json` beside them.
