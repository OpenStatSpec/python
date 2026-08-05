# Dataset transformations

OpenStatSpec separates transformation syntax, canonical meaning, and database
mutation. This lets multiple language frontends produce the same plan without
coupling the executor to any one language.

The implemented bounded SPSS-like frontend accepts `RECODE`, sequential
`COMPUTE` and `IF`, `VARIABLE LABELS`, `VALUE LABELS`, numeric `FORMATS`,
`VARIABLE LEVEL`, and `EXECUTE`. Predicates support typed variable/literal
operands, parentheses, numeric comparisons, `AND`, and `OR`. String comparison
and v0.2 string assignment fail closed until exact profile-independent
collation and explicit-width semantics are available; arbitrary SPSS, Python,
and SQL expressions are rejected. Stata and SAS are not implemented.

## Architecture

The path has three layers:

1. A **frontend** parses source text and binds names and types against an
   explicit input schema. It performs no database mutation or SQL generation.
2. The **canonical plan** contains ordered, typed, language-neutral operations.
   Its canonical JSON and hash are independent of SQL dialect and source
   formatting.
3. The **in-place executor** validates the complete plan against the live
   dataset, then directly mutates that dataset's existing wide table and
   metadata catalog. It does not parse frontend syntax.

This is a trust boundary. JSON plans must pass
`transformation_plan_from_dict()` and live-schema validation before mutation;
they are never treated as arbitrary SQL.

## Install the audit schema

Install the compact audit relation once before the first apply:

```python
import openstatspec

openstatspec.install_in_place_transformation_schema(
    database_url="sqlite:///survey.sqlite",
)
```

Installation is separate because schema DDL may commit independently on some
engines. Apply fails before changing data or metadata when the audit relation is
absent, and never creates or migrates schema-management objects itself.

The CLI equivalent is:

```text
openstatspec install-in-place-schema --database-url sqlite:///survey.sqlite
```

## Generic canonical-plan apply

`openstatspec.transform.TransformationPlan` is the generic model. Numeric
constants retain exact binary64 bits, strings retain exact Unicode text, and
operation order is significant. Use `transformation_plan_from_dict()` for
untrusted mappings. `canonical_plan_json()` and `canonical_plan_hash()`
produce stable audit identities.

The public API is:

```python
result = openstatspec.apply_transformation_plan_in_place(
    database_url="sqlite:///survey.sqlite",
    dataset_id="responses",
    plan=plan,
    actor="agent@example.org",
    expected_branch=None,
    expected_head=None,
)
```

`plan` accepts a `TransformationPlan` or a mapping handled by the strict
loader. The executor recomputes the hash and validates the whole plan before
mutation.

The corresponding CLI is:

```text
openstatspec apply-plan --database-url sqlite:///survey.sqlite \
  --dataset-id responses --actor agent@example.org --plan-file plan.json
```

## SPSS-like frontend

The pure compiler works without a database:

```python
from openstatspec import VariableDefinition, VariableSchema, compile_spss_syntax

schema = VariableSchema((VariableDefinition("age", "numeric"),))
compilation = compile_spss_syntax(
    "RECODE age (18 THRU 34 = 1) (35 THRU 64 = 2).",
    schema,
)
print(compilation.plan.canonical_json())
print(compilation.plan_hash)
```

It normalizes line endings, records the source hash, parses and sequentially
binds the supported subset, and returns a canonical plan plus output schema.

For database-connected use, the compatibility wrapper loads the live schema,
compiles the source, and invokes the in-place path:

```python
result = openstatspec.apply_spss_in_place(
    database_url="sqlite:///survey.sqlite",
    dataset_id="responses",
    actor="agent@example.org",
    source_text="""
      COMPUTE target = 0.
      IF (source_a = 1 AND source_b = 1) target = 1.
      VARIABLE LABELS target 'Example label'.
      VALUE LABELS target 0 'No' 1 'Yes'.
      FORMATS target (F1.0).
      VARIABLE LEVEL target (NOMINAL).
      EXECUTE.
    """,
)
```

`openstatspec.compile_spss_syntax` and
`openstatspec.apply_spss_in_place` remain compatibility APIs.

The current CLI wrapper is:

```text
openstatspec apply-spss --database-url sqlite:///survey.sqlite \
  --dataset-id responses --actor agent@example.org --syntax-file transform.sps
```

On Dolt, also pass `--expected-branch` and `--expected-head`.

## Database and Dolt invariants

Every successful apply preserves the logical `dataset_id` and physical
schema/table identity. It creates no derived dataset, output table, full-table
copy, staging table, snapshot, rollback artifact, or recovery/history layer.
Assignments and recodes use ordered `UPDATE` statements; later operations see
earlier results. Label, value-label, format, and measurement-level operations
update the normative catalog.

A numeric create target is supported atomically on SQLite and PostgreSQL.
MySQL, MariaDB, and Dolt reject `target_mode=create` before mutation. On those
profiles a separate versioned stage must first provision the nullable numeric
physical column and normative variable row; the transformation executor then
sees a pre-existing target and performs no schema DDL.
The public operation reports success only after physical data, normative
metadata, and the compact audit row are mutually complete.

Before Dolt mutation, the executor verifies the expected branch and `HEAD` and
requires clean `dolt_status`. Success changes the same working set without
changing `HEAD`; OpenStatSpec does not call `DOLT_COMMIT`, `DOLT_RESET`, switch
branches, merge, tag, or create a hidden recovery commit. It rechecks branch,
HEAD, and a clean working set after locking the dataset and immediately before
mutation. The caller reviews a successful `dolt diff` and
separately decides whether to commit or restore.

## Audit and provenance

Each success writes one compact `transformation_apply` row in the data and
metadata transaction. It records dataset and relation identity, actor, plan
hash, operation count, timestamps, database profile, and relevant Dolt
branch/HEAD. SPSS source also has a normalized source hash.

The row stores no case values and references no copied state. It is provenance,
not an undo log or substitute for Dolt history. `source_kind` distinguishes a
direct `canonical_plan` from `spss_syntax`. For a direct plan, the canonical
JSON document is itself the source artifact, so its source hash equals the plan
hash and `frontend_contract` is null. SPSS applies record the normalized
syntax hash and SPSS frontend contract.

## Package layout

The intended boundary is:

```text
openstatspec
├── transform
│   ├── plan.py
│   ├── schema.py
│   ├── validation.py
│   └── errors.py
└── frontends
    ├── spss
    │   ├── syntax.py
    │   ├── binding.py
    │   ├── compiler.py
    │   └── execution.py
    ├── stata       # empty placeholder; not implemented
    └── sas         # empty placeholder; not implemented
```

Current imports from `openstatspec` and `openstatspec.transform` remain
compatibility contracts if implementation files move. Stata and SAS provide no
parser, compiler, apply API, CLI choice, or support claim.

## Extension guidance

There is no plugin discovery or Python entry-point protocol. A future built-in
frontend must parse with source spans, bind against `VariableSchema` without
database access, emit only canonical operations, produce deterministic hashes,
share the generic validator/executor, and add specification-owned conformance
fixtures.

An external plugin protocol should be added only for a real external frontend.
It would need explicit identity, contract compatibility, deterministic
compilation, capabilities, stable error semantics, and a trust policy.

Transformation conformance covers canonical plan fixtures, strict invalid-plan
cases, frontend source/plan hashes and diagnostics, wrapper-to-plan equivalence,
preflight-before-mutation, dataset/table identity, metadata and audit rows, CLI
compatibility, and service evidence before database execution support is
claimed. Stata and SAS need their own fixtures and implementations before their
placeholder status can change.
