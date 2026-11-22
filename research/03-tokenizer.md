# 03 - Tokenizer tables, TokenType, class-creation hook

Pin: sqlglot eb11aec. Everything below is from reading files and inspecting `maps/impact.json.gz`/`sample/`. Nothing was run.

## (a) Findings

1. **There is no `tokenizers/` directory and no metaclass at the pin.** `ls work/sqlglot/sqlglot/` shows `tokens.py`, `tokenizer_core.py`, `dialects/`, `parsers/`, `generators/`, `typing/`, but no `tokenizers/`. `selector.py:23` lists `"sqlglot/tokenizers/"` in `DIALECT_DIRS`. That entry is dead and harmless. Dialect tokenizers are nested `class Tokenizer(tokens.Tokenizer)` blocks inside `sqlglot/dialects/<d>.py` (55 hits, e.g. `dialects/snowflake.py:131`). The executor and jsonpath also subclass `Tokenizer` (`executor/python.py:554`, `jsonpath.py:15`).
2. **Where the tries are built.** `tokens.py:84-126` is `_TokenizerBase.__init_subclass__`. It builds `_QUOTES`, `_IDENTIFIERS`, `_FORMAT_STRINGS`, `_STRING_ESCAPES`, `_COMMENTS` and `_KEYWORD_TRIE` (`new_trie(...)` at 117). It runs once per Tokenizer subclass, at class definition (import time). `tokens.py:32-39` (`_convert_quotes`, `_quotes_to_format`) are helpers it calls. The metaclass `_Dialect.__new__` (`dialects/dialect.py:306-432`) does the same job for dialects. It sets `tokenizer_class` (337) and derives `ESCAPED_SEQUENCES` from `NUMERIC_ESCAPES` (404-419).
3. **Tables are plain class attributes** (`tokens.py:129` SINGLE_TOKENS, `:194` NUMERIC_ESCAPES, plus KEYWORDS, QUOTES, STRING_ESCAPES and the rest). `Tokenizer._init_core` (`tokens.py:576-610`) hands them to `TokenizerCore`. Any commit that edits a table line falls in the "class body" branch of `selector.py:310-326`, so it goes to `import_time_change` (`selector.py:250`).
4. **Consumers of the tables outside the tokenizer.**
   - `parser.py:690` builds `RESERVED_TOKENS` from `Tokenizer.SINGLE_TOKENS.values()` at class-body time.
   - `dialect.py:404-421` reads `NUMERIC_ESCAPES` and `KEYWORDS["(+)"]` at dialect creation.
   - `generator.py:1723` and `:3283` read `NUMERIC_ESCAPES` and `DROP_UNKNOWN_ESCAPES` during generation.
   - The tests reference these tables almost nowhere (`grep KEYWORDS|SINGLE_TOKENS|STRING_ESCAPES tests/` returns one comment hit). Tests observe table edits only through SQL strings.
5. **Coverage of import-time code is empty or misleading.**
   - `tokens.py` class-body lines 128-198 have empty sets (set id 0). This includes the NUMERIC_ESCAPES line 194.
   - `tokenizer_core.py` lines 1-39 and the whole TokenType enum (15-476) are also empty.
   - The dialect class-body lines are not empty. `snowflake.py:131-138` is covered by 4 tests. Those are the tests during which snowflake was first lazily imported.
   - `tokens.py:85-124` (the `__init_subclass__` body) is covered by **only 65 tests**, and `dialect.py:306-432` by 65/45/33. Those are the tests that happened to trigger a lazy dialect import. This is order-dependent and not meaningful.
6. **Hot vs cold in the core.**
   - 172 of 1073 covered lines in `tokenizer_core.py` are hit by about 1559 or more tests (all but the tests that never tokenize).
   - `tokens.py` has 38 such lines (the `_init_core` body 577-610, 615).
   - `file_cov(tokens.py)` is 1567 tests / 102.5 s. `file_cov(tokenizer_core.py)` is 1565 tests.
   - Cold branches are narrow. Examples: `_read_numeric_escape` line 1244 is hit by 6 tests (bigquery), and `_scan_numeric_escape` line 1363 by 23.
7. **How the selector handles each shape.**
   - Edit inside a `tokenizer_core.py` function: window coverage (`selector.py:300-309`). Hot line gives about 1564 tests, cold line gives a few. Fine, and correct.
   - Edit to a table line in `tokens.py` or to a TokenType member: `import_time_change`. The family is `{tokens.py}`, so `file_cov` is about every test. Add `name_tests({"tokens"})`, which is test_tokens. This is the 102 s ceiling. It is recall-safe and expensive.
   - Edit to a dialect Tokenizer table line: family = dialect files plus importing subclass dialects, plus `name_tests`, plus `class_tests(<TopClass>)`.
     - For snowflake this is about 357 tests / 11.0 s, which costs about 6.6 s once split.
     - The biggest contributors are `test_annotate_funcs` 1.96 s and `test_qualify_columns` 1.41 s (optimizer fixtures), then `test_snowflake` 0.66 s.
8. **Why sample 4d63 failed nothing (derived from the pinned tests, not tuned).** The patch changes the base of snowflake's `"u"` row from 16 to 17 (`dialects/snowflake.py:133`).
   - That row is read only by `_read_numeric_escape` (`tokenizer_core.py:1194-1211`, `_DIGIT_VALUES.get(ch, 16)` against `base`).
   - It is reached only when a snowflake-tokenized string literal contains `\u`.
   - No test does that. `grep '\\u[0-9a-f]{4}' tests/` hits only `test_jsonpath` (a different tokenizer), `test_presto` 777-787 and `test_spark` 88. Presto's hits are `write={"snowflake": ...}` expectations: snowflake is a generation target and never tokenizes the string. Spark's `A` goes through Spark's own tokenizer. The one fixture hit is `optimizer/annotate_functions.sql`, which tokenizes under whatever dialect it names, and the `\u` there does not involve a snowflake string with those digits.
   - `dialect.py:404-419` only checks key presence (`"x" in`), not bases, so the value change leaves generation unchanged.
   - The edit is behaviourally unobservable in the suite. The selector rightly picked 357 tests and none failed.
   - General lesson: a value-only change in a numeric-escape row is observed only by tests that decode that escape letter in that dialect. The `.sql`/`.json` fixtures that contain the literal sequence are the only other way to see it.

## (b) Recall risks and cost waste

**R1 (HIGH, recall): edits inside `_TokenizerBase.__init_subclass__` or its helpers are under-selected.**
- This is `tokens.py:32-39` and `:84-126`. `selector.py:299-309` treats them as ordinary functions, so it takes window coverage. That coverage is the 65 accidental lazy-import tests (finding 5).
- A real break here, such as a wrong `_KEYWORD_TRIE` condition, a typo, or a dropped `_FORMAT_STRINGS` entry, either raises at `import sqlglot` or breaks every dialect's tokenization. Nearly every test fails, so 1,500 or more tests would be missed.
- The same applies to `_Dialect.__new__` (`dialect.py:306-432`) and to any function called from a class body or module level (for example module-level helper calls in `dialects/*.py`).
- The class-body branch (`selector.py:310-326`) is only reached when `innermost` finds no function. A function that runs at import time is wrongly treated as runtime code.
- Only `__init_subclass__`/`__new__` editing patch shapes are at risk. Whether the hidden set contains one is unknown, but it is the cheapest "mutate the hook" shape for any mutation corpus.

**R2 (MED, recall): a TokenType member removed or renamed.**
- References sit in class-body tables (parser `FUNCTIONS`, `TYPE_TOKENS`, dialect `KEYWORDS`). The members do not execute during a test, so their coverage is empty.
- Any remaining reference raises AttributeError at import. Currently this goes through `import_time_change` and `file_cov(tokenizer_core)`, which is about all tests. That is correct, so keep it. Do not add a "references-only" narrowing for the enum (see recs).
- A pure addition at the end is harmless. An addition in the middle shifts every later `auto()` value. I found no code that depends on the numeric values, since `TokenType(` and `.value` have no consumers. It is safe but unproven.

**R3 (LOW, recall): `DIALECT_DIRS` entry `sqlglot/tokenizers/` does not exist.** Harmless today. Do not rely on it for tokenizer edits.

**C1 (cost): any class-body edit to `tokens.py` or any module-level edit in `tokenizer_core.py` selects everything (102 s, so about 45 s of simulated cost per commit after packing).**
- This includes the module constants `_CHAR_UPPER`, `_DIGIT_CHARS`, `_DIGIT_VALUES`, `_OCTAL_CHARS` (`tokenizer_core.py:9-12`), which are used only in a few functions.
- Selection goes through `import_time_change` even though only specific functions read them.

**C2 (cost): dialect tokenizer table edits select the whole dialect family, about 11 s for snowflake.**
- About 350 tests are chosen. Tests that do not tokenize the changed entry can fail only if they use the dialect, and most do not exercise the edited key.
- This is a minor cost (about 4 s per commit). Do not trade recall for it.

## (c) Proposed changes

### P1 (recall fix): treat import-time functions as import-time changes

`tools/build_index.py`: add to the dict returned by `build()`. A function is import-time if it is called from module or class level (outside any def). It is also import-time if it is a hook (`__init_subclass__`, `__set_name__`, or `__new__` in a class with base `type`), or if it is called by an import-time function in the same file.

```python
# inside build(), before `return`
import_fns: dict[str, list] = {}
for rel, tree in trees.items():
    defs = {}  # name -> list of nodes (same file)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.setdefault(node.name, []).append(node)

    def call_names(n):
        for c in ast.walk(n):
            if isinstance(c, ast.Call):
                f = c.func
                yield f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None

    seeds = set()
    def scan(node, in_fn):  # calls made outside any def
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if ch.name in ("__init_subclass__", "__set_name__"):
                    seeds.add(ch.name)
                continue
            if isinstance(ch, ast.ClassDef) and any(
                (b.id if isinstance(b, ast.Name) else None) == "type" for b in ch.bases
            ):
                seeds.add("__new__")
            if isinstance(ch, ast.Call):
                f = ch.func
                seeds.add(f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else "")
            scan(ch, in_fn)
    scan(tree, False)
    todo, hot = list(seeds), set()
    while todo:
        n = todo.pop()
        if n in hot or n not in defs:
            continue
        hot.add(n)
        for d in defs[n]:
            todo.extend(x for x in call_names(d) if x)
    # a def nested in a function is still the same def range; ranges are enough
    import_fns[rel] = [[d.lineno, d.end_lineno] for n in hot for d in defs[n]]
```

Add `"import_fns": import_fns` to the returned dict. Seeds are over-inclusive. Names like `dict` or `new_trie` that are not defined in this file are dropped by the `n not in defs` check. Method names that are common across a file (for example `_parse_x` called at import) would also be pulled in. The cost is some extra widening for functions of that name in the same file only.

`selector.py`: add a helper next to `lambda_range` (`selector.py:243`):

```python
    def in_import_fn(self, rel: str, line: int) -> bool:
        return any(a <= line <= b for a, b in self.index.get("import_fns", {}).get(rel, []))
```

And in `sqlglot_change`, replace `if fn:` (`selector.py:300`) with:

```python
            if fn and self.in_import_fn(rel, ln):
                # Runs at class creation / import: coverage of this function is whoever imported first.
                hit = self.import_time_change(rel, ln)
                if not hit:
                    return set(self.all), "import-fn-unmapped"
                out |= hit
                reasons.append("import-fn")
            elif fn:
```

(keep the rest of the `if fn:` body as the `elif` body unchanged). Effect: an edit to `__init_subclass__` or `_Dialect.__new__` now takes `file_cov(tokens.py)` (about 1567 tests), so it is recall-safe. It costs about one extra high-cost commit in a corpus that contains such a patch.

### P2 (cost): constants private to a module resolve to their readers

`selector.py`, in `sqlglot_change` at the class-body/module-level branch (after the `lam` check, before `rel.endswith("__init__.py")`). Applies only when the edited statement is a module-level assignment to a `_Private` name and every use of the name is inside a function of the same file. Otherwise fall through to `import_time_change`:

```python
                name = module_private_name(old_tree, ln)   # new helper below
                if name:
                    users = name_uses(new_tree, name) | name_uses(old_tree, name)  # [(start,end,in_fn)]
                    if users and all(in_fn for _s, _e, in_fn in users) and not self.index.get("name_files", {}).get(name, [rel]) != [rel]:
                        hit = set()
                        for s, e, _ in users:
                            hit |= self.cov(rel, range(s, e + 1))
                        if hit:
                            out |= hit
                            reasons.append("const")
                            continue
```

```python
def module_private_name(tree, line):
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.lineno <= line <= node.end_lineno:
            tgt = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if isinstance(tgt, ast.Name) and tgt.id.startswith("_") and not tgt.id.startswith("__"):
                return tgt.id
    return None


def name_uses(tree, name):
    out = set()
    if tree is None:
        return out

    def walk(node, in_fn):
        for ch in ast.iter_child_nodes(node):
            fn = in_fn or isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            if isinstance(ch, ast.Name) and ch.id == name and not isinstance(ch.ctx, ast.Store):
                out.add((ch.lineno, ch.end_lineno or ch.lineno, in_fn))
            walk(ch, fn)

    walk(tree, False)
    return out
```

`name_files` comes from `build_index.py`. It is `{name: [rel,...]}` for `_`-prefixed module-level assignment targets, taken from a `tokenize` NAME scan over `texts` (same loop that builds `refs`), so a `from x import _name` elsewhere falls back. Edit lines inside the multi-line assignment are already widened to the statement's first line by `changed_statement_lines`. Effect: `_DIGIT_VALUES` edits go from 1565 tests to the numeric-escape readers (about 6-23 tests). Do not extend this to the `TokenType` enum (see R2).

### P3 (optional cost, low priority): value-only numeric-escape rows use the escape letter as a needle

Only when (1) every changed line sits inside a `NUMERIC_ESCAPES = {...}` assignment of a class named `Tokenizer`, and (2) the old and new dict have the same key set (compare via `ast.literal_eval` on `Dict.keys`; values only through `ast.dump`). Then the changed keys K are the keys whose value dump differs, and the candidates are the existing `import_time_change` result filtered by:

```python
needles = [("\\" + k) if k != "0" else None for k in K]          # "\\u", "\\x"; "0" means \0-\7 digits
def sees(strings):  # strings from self.strings[n], plus fixture text read only now
    return any(("\\" in s) if None in needles else any(nd in s for nd in needles if nd) for s in strings)
```

Add every test that loads a fixture file containing the needle (read `tests/fixtures/**` once, only on this path, and use `fixture_change`'s stem lookup). Add `tests/test_tokens.py` and `tests/dialects/test_<family>.py` unconditionally. A key added or removed, or any other table (QUOTES, STRING_ESCAPES), keeps the current behaviour. The key set drives `dialect.py:404-419` and generator 1723/3283, which change generation. Saving: about 4 s per such commit (357 tests to about 8; fixture loaders such as `test_annotate_funcs` 1.96 s stay). This is not worth it unless P1/P2 are done first.

## (d) Ranked recommendations

1. **P1 (import-time function detection), recall.** It removes the only concrete miss I found: `__init_subclass__`/`_Dialect.__new__` edits currently map to 65 accidental tests. It adds cost only when such an edit occurs, and then only where it is correct. Implementation is about 40 lines in the index and about 10 in the selector.
2. **Keep the current handling of `TokenType` members and class-body table edits in `tokens.py` (all-tests).** Do not add key-needle narrowing for base `KEYWORDS`/`SINGLE_TOKENS`/`QUOTES`. Needle matching would miss fixture-driven tests, programmatic SQL, generation-side effects (`dialect.py:404`) and the `parser.py:690` reserved-token set. The savings are about 40 s on a rare commit. It is the wrong place to take risk.
3. **P2 (module-private constants resolve to readers), cost.** This is a safe narrowing of the "module-level, unmapped" path for `tokenizer_core.py` constants (1565 tests down to under 25). It is useful beyond the tokenizer, for any `_CONST` edit in `helper.py`, `time.py` and similar.
4. **Remove the dead `"sqlglot/tokenizers/"` entry from `DIALECT_DIRS`, or leave it.** It has no effect at this pin, and it is safe either way.
5. **P3 (value-only NUMERIC_ESCAPES), cost only.** About 4 s per matching commit, with a small recall-reasoning surface. Do it last, or skip. Do not apply it to key-set changes.
6. **Do not special-case sample 4d63.** Its zero-failure outcome comes from the missing snowflake `\u` test (finding 8). It would not be a safe signal for any future commit.

Expected impact: P1 only changes outcomes for the rare class-creation-hook patch, but that one is a possible miss of 300 s. P2 is a possible saving of about 30-45 s on module-constant edits in the core. P3 saves about 4 s per matching commit.
