# Dataset transformations

OpenStatSpec separates transformation syntax, canonical meaning, and database
mutation. This lets multiple language frontends produce the same plan without
coupling the executor to any one language.

The implemented bounded SPSS-like frontend accepts bounded `STRING`
declarations, `RECODE`, sequential `COMPUTE` and `IF`, `DELETE VARIABLES`,
`VARIABLE LABELS`, `VALUE LABELS`, numeric `FORMATS`, `VARIABLE LEVEL`, and
`EXECUTE`. Predicates support typed variable/literal
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

## Contract ownership and legacy compatibility

Published OpenStatSpec `v0.5.0` defines Transformation Plan 0.1/0.2, not
Plan 0.3. Its optional SPSS Frontend 0.3 is a syntax-only expansion emitting
Plan 0.1/0.2, implemented through the explicit official request boundary below.

Explicit `create_variable` / `delete_variable` operations (including SPSS
`STRING` / `DELETE VARIABLES`) belong to a **Python extension**, not official
OpenStatSpec conformance. New schema-changing compilations emit:

- Plan: `openstatspec-python-schema-change-plan-v0.1`
- Frontend: `openstatspec-python-schema-change-spss-v0.1`

The exported names `TRANSFORMATION_PLAN_SCHEMA_CHANGE_CONTRACT` and
`SPSS_FRONTEND_SCHEMA_CHANGE_CONTRACT` remain unchanged; their values now use
these Python-owned identifiers. Plans without these explicit schema operations
retain their existing Plan 0.1/0.2 selection and default frontend identifier. Official
Plan 0.1/0.2 still reject explicit create/delete operations.

The loader and executor continue to accept the old Python plan identifier
`openstatspec-transformation-plan-v0.3` with its original operation semantics.
This is legacy compatibility, **not** recognition of an official Plan 0.3.
Loading, canonical serialization, hashing, and apply preserve the supplied
identifier; existing canonical JSON, hashes, and stored audits are not rewritten.
Historical `openstatspec-spss-syntax-frontend-v0.3` audit values described this
Python extension and are not evidence of official Frontend 0.3 conformance.

No database migration is needed. Consumers should accept the new extension IDs
before using new compiler output. Keep stored legacy plans and audits intact;
recompiling schema-changing syntax now produces a new plan identity/hash even
when its operations are identical. An intentional change of a saved plan's
contract likewise creates a new artifact with a new hash, not an audit migration.
Older adapter versions cannot load the new IDs.

## Official Frontend 0.3

Use `openstatspec.compile_spss_request(request)` for the official JSON request
boundary. It accepts **only** `openstatspec-spss-syntax-frontend-v0.3`, with
exact required fields `contract`, `input_alias`, `input_schema`, and
`source_text`. Unknown fields, missing fields, wrong types, noncanonical or
nonfinite typed codes, ambiguous variable names, and mismatched label types
fail closed. Request-shape failures report `invalid_spss_request`; source
failures retain the normative diagnostics. It returns no partial compilation.

```python
compilation = openstatspec.compile_spss_request({
    "contract": "openstatspec-spss-syntax-frontend-v0.3",
    "input_alias": "parent",
    "input_schema": {"variables": [
        {"name": "age", "storage_kind": "numeric"},
    ]},
    "source_text": "COMMENT finite ages. RECODE age (LOWEST THRU 17 = 0).",
})
```

The ordered request dictionary accepts only `name`, `storage_kind`,
`variable_label`, ordered typed `value_labels`, `format_family`, `width`,
`decimals`, and `measurement_level`. In particular, `width`/`decimals` are
request field names, not `format_width`/`format_decimals`. Descriptive input
format metadata is preserved, including partial metadata and string formats;
`FORMATS` operations still accept only bounded numeric F formats. Physical
identifiers and Python `declared_string_width` are not request fields.

The same parser/binder implements command-boundary star and `COMMENT`
comments, non-nested block comments, dictionary-order inclusive `TO` ranges,
grouped recodes/labels/formats/levels, finite `LOWEST`/`HIGHEST` RECODE bounds,
`NE`/`<>`/`~=`, `NOT`, and `ADD VALUE LABELS`. `TO` cannot generate `INTO`
targets. Additive labels update an existing typed code at its ordinal and
append new codes in source order, using the preceding label state. Numeric
zero has positive-zero identity; string codes retain exact contents.
Comments remain in the LF-normalized source hash but emit no operation;
comment-only input is invalid. Python `STRING` and `DELETE VARIABLES` are
rejected under the official selector.

**Explicit implementation decision:** precedence is standard SPSS order,
comparisons → `NOT` → `AND` → `OR` (tightest first). Thus `NOT a = 1 AND
b = 1` means `(NOT (a = 1)) AND b = 1`, not negation of the conjunction.
Parentheses override precedence. Comparison complements and De Morgan lowering
preserve SQL UNKNOWN, and maximal same-operator nodes flatten in source order.
This decision has adapter regression tests; normative fixtures are unchanged.

Programs using only Plan 0.1 operations retain their exact Plan 0.1 object;
any Plan 0.2-only operation selects Plan 0.2. Both source and complete canonical
plan hashes remain independent. Official compilations record the 0.3 frontend
identifier regardless of output-plan version.

For typed-schema or live-database callers, explicitly pass
`frontend_contract="openstatspec-spss-syntax-frontend-v0.3"` to
`compile_spss_syntax` or `apply_spss_in_place`. The latter compiles against the
live schema within the existing transaction and records official frontend and
source/plan provenance. JSON callers should use `compile_spss_request` rather
than building a typed schema from untrusted fields. Omitting the selector
retains all old support, rejection behavior, and Python extension selection;
the existing CLI remains on that compatibility path.

Evidence: `tests/test_frontend_v03.py` runs the 35 declared and all 90 effective
cases from specification commit
`864e84479f554b8ee250ffed44c4dfb963750d4a`, applying only the published inherited
contract overrides and two comment supersessions. It checks exact Plan 0.1/0.2
objects/hashes, source hashes, diagnostics, and declared output metadata.
Local SQLite integration checks UNKNOWN, sequential data and metadata,
identity, audit provenance, and absence of copy/history artifacts. Existing
frontend, plan, and in-place suites remain in the gate. This is not new service
execution evidence: MySQL/MariaDB/Dolt provisioning restrictions and caller-owned
Dolt commits are unchanged.

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

## Numeric targets: create versus replace

`COMPUTE` chooses its canonical target mode from the live schema. An absent
numeric target lowers to `assign` with `target_mode=create`; it appends one
nullable numeric column and one catalog variable. An existing numeric target
lowers to `target_mode=replace`. Replace keeps the variable ID, ordinal, and
metadata that no later operation explicitly changes. `IF` requires an existing
target, so a new target must be initialized first. Variable-source `NULL`
propagates through assignment; a predicate changes a row only when its SQL
truth value is `TRUE`.

For example, this is a create-or-replace program for a numeric target. On
SQLite/PostgreSQL it creates `target` when absent; with a pre-existing numeric
`target` it updates the same target instead:

```text
COMPUTE target = source_a.
IF (source_a = 1 AND source_b = 1) target = 1.
IF (target = 1 AND source_b = 1) target = 2.
VARIABLE LABELS target 'Numeric target'.
VALUE LABELS target 0 'No' 1 'Yes'.
FORMATS target (F8.1).
VARIABLE LEVEL target (NOMINAL).
EXECUTE.
```

`RECODE ... INTO` always requests a new target and rejects an existing target
name. A canonical replace recode must use the same source and target name.
Without `ELSE`, create recodes use `system_missing` for unmatched rows, while
replace recodes use `copy`. To pre-provision `band` and preserve the create
semantics, initialize and recode it explicitly. The absent-target form is:

```text
RECODE source_a (1 = 0.1) (2 = 1) INTO band.
```

The pre-existing-target form is:

```text
COMPUTE band = source_a.
RECODE band (1 = 0.1) (2 = 1) (ELSE = SYSMIS).
```

The second form has an extra ordered assignment and is not a pure DDL-overhead
comparison. Omitting its `ELSE` would copy unmatched source values instead of
matching the create form. For canonical plans, create recodes use
`target_mode=create` and replace recodes use `source=target`,
`target_mode=replace`.

On SQLite and PostgreSQL, numeric create is supported only where the adapter
has the atomic native transaction boundary. MySQL, MariaDB, and Dolt reject a
create target with `schema_change_not_atomic`. A caller must use a separate,
explicit, versioned provisioning action that creates both the nullable physical
column and its matching normative variable, including a unique variable ID,
the same physical binding, and the next ordinal. Provisioning is not an apply
step, and this package does not hide it behind a new helper or API. See the
specification's [In-Place Transformation Binding 0.2 target-creation
rules](https://github.com/OpenStatSpec/specification/blob/main/docs/transformation-plan-sql-binding-0.2.md#target-creation-by-sql-profile).
For Dolt, commit provisioning separately; the later apply starts from that
clean committed `HEAD` and supplies it as `expected_head`. The SQLite matrix
below is local evidence only and is not service or cross-engine evidence.

## Local numeric-target measurement

The following is a bounded observation of the existing workflow, not a
release-to-release benchmark. A disposable standard-library harness created a
fresh file-backed SQLite database for every variant and repetition, kept
fixture setup separate from the timed public call, and removed all temporary
files afterward. It covered two workloads (A: the `COMPUTE`/`IF` program above;
B: `RECODE`), create versus pre-provisioned replace, 100 versus 100,000 cases,
and both `apply_transformation_plan_in_place` and `apply_spss_in_place`. Each
cell had one counted fresh apply, one fresh warm-up, and five fresh timed
applies; create/replace order alternated by repetition. Each counted,
warm-up, and timed apply started from a newly created fixture database; no
apply reused a database from another trial. Rows were generated by cycling
this input sequence in order: `(1,1)`, `(1,NULL)`, `(NULL,1)`, `(0,1)`,
`(2,2)`, `(0.1,0)`, `(-9,1)`, `(NULL,0)`. Thus the 100-row runs used 12 full
cycles plus the first four rows of a cycle, while the 100,000-row runs used
12,500 full cycles. Each fixture had numeric `source_a` and `source_b`; `source_a` carried
label `Source A` and missing rule `99`, while `source_b` carried label
`Source B` and no missing rule. Replace fixtures added one nullable numeric
ordinal-3 target (`target` for A, `band` for B) with label `Old target`, `F4.0`
formats, ordinal measurement level, and missing rule `99`; create fixtures had
no target column.

The counted value is an outermost SQLAlchemy `Connection.execute` or
`Connection.exec_driver_sql` invocation scoped to the owned database. It
includes profile probes, catalog checks, reflection, explicit `BEGIN`, DDL,
DML, and audit work; one executemany call counts once. It is not a driver wire
round-trip count. Timed values are the public apply call only: setup,
validation, and cleanup are excluded. Every counted and timed result checked
ordered rows, explicit `NULL`s, binary64 bits (including `0.1`), source and
target identity, catalog/audit provenance, and absence of forbidden copy or
snapshot artifacts before being retained.

Environment and revisions: Python 3.13.2, SQLite 3.47.1, SQLAlchemy 2.0.52,
Linux 6.6.87.2-microsoft-standard-WSL2 on x86_64 with 12 logical CPUs and
local temporary-file storage; observed file settings were
`journal_mode=delete` and `synchronous=2` (`FULL`). The Python source was
`63a66d6589aeb8fdc4820d5e7217fc13c61ef3b9`; the reviewed specification
reference was `930345b922af4f43b0e622be02d4b12ebfeb08eb`. Fixture setup was
recorded separately and excluded from the table.

| Workload | Cases | API | Mode | SQLAlchemy executes | Median ms | Min ms | Max ms |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: |
| A | 100 | canonical plan | create | 234 | 76.5 | 65.8 | 78.4 |
| A | 100 | SPSS | create | 237 | 69.3 | 66.8 | 78.2 |
| A | 100 | canonical plan | replace | 222 | 67.6 | 56.6 | 88.3 |
| A | 100 | SPSS | replace | 225 | 71.3 | 69.3 | 88.5 |
| A | 100,000 | canonical plan | create | 234 | 168.8 | 134.1 | 172.3 |
| A | 100,000 | SPSS | create | 237 | 160.2 | 149.4 | 170.3 |
| A | 100,000 | canonical plan | replace | 222 | 162.7 | 146.0 | 173.6 |
| A | 100,000 | SPSS | replace | 225 | 155.4 | 144.8 | 177.6 |
| B | 100 | canonical plan | create | 224 | 61.6 | 55.6 | 76.6 |
| B | 100 | SPSS | create | 227 | 77.1 | 65.5 | 312.8 |
| B | 100 | canonical plan | replace | 213 | 64.2 | 60.2 | 223.1 |
| B | 100 | SPSS | replace | 216 | 80.9 | 62.2 | 237.5 |
| B | 100,000 | canonical plan | create | 224 | 149.4 | 131.7 | 193.4 |
| B | 100,000 | SPSS | create | 227 | 144.1 | 128.3 | 158.4 |
| B | 100,000 | canonical plan | replace | 213 | 169.8 | 163.6 | 186.6 |
| B | 100,000 | SPSS | replace | 216 | 192.8 | 171.4 | 202.8 |

The canonical plan operation/hash identities for the same runs were:

| Workload | Mode | Operations | Contract | Canonical plan SHA-256 |
| --- | --- | ---: | --- | --- |
| A | create | 8 | v0.2 | `6b17a220f9be9d2d29b671dd5eb4598ea34ae459e4d3b8e84a7daafbe3ec11ef` |
| A | replace | 8 | v0.2 | `a239cac39d3fade1207228bee4296a444669eb31e154e6cea09f378ea75b12eb` |
| B | create | 1 | v0.1 | `2057e57117100ba3efd5becf5254f47a5696bc8631c35480a5a6d2f5b1327f83` |
| B | replace | 2 | v0.2 | `23b4bbf06383c5e73e04356af079db1e6fe0827f8afdb406f849169306fa2214` |

These observations do not establish service latency, wire round trips,
row-independent cost, or a before/after speedup for any release. They are
owned SQLite evidence for the documented behavior at the revisions above.

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
