# SQL transformation workflow

OpenStatSpec's optional SQL workflow keeps imported source datasets immutable.
A SQL result is recorded in the separate
`openstatspec-sql-transformation-workflow-v0.1` catalog and is never inserted
into the source-faithful core `dataset` relation.

This milestone implements one workflow backend: SQLite. PostgreSQL,
MySQL, and MariaDB remain supported by the core import/export catalog, but SQL
workflow registration, execution, derived-catalog access, and cleanup fail
closed with `dialect_not_supported` on those engines.

## Public API

First obtain the UUID of a core dataset. The import API's caller-supplied name
is not a public catalog UUID.

```python
from openstatspec import list_datasets

catalog = list_datasets(
    database_url="sqlite:///survey.sqlite",
    kind="core",
)
parent_id = catalog["datasets"][0]["dataset_id"]
```

Register and execute a materialized transformation:

```python
from openstatspec import derive_sql_dataset

derived = derive_sql_dataset(
    database_url="sqlite:///survey.sqlite",
    parent_dataset_id=parent_id,
    transformation_name="eligible_responses",
    query_sql="""
        SELECT respondent_id, weight, answer
        FROM parent
        WHERE age >= :minimum_age
        ORDER BY respondent_id ASC NULLS LAST
    """,
    parameters={"minimum_age": 18},
    columns=[
        {
            "name": "respondent_id",
            "storage_kind": "numeric",
            "source": "respondent_id",
            "lineage_kind": "identity",
        },
        {
            "name": "weight",
            "storage_kind": "numeric",
            "source": "weight",
            "lineage_kind": "identity",
        },
        {
            "name": "answer",
            "storage_kind": "numeric",
            "source": "answer",
            "lineage_kind": "identity",
        },
    ],
    weight_variable="weight",
    row_semantics="filter",
    metadata_policy="declared",
)
```

The same operation can be split into
`register_sql_transformation(...)` and
`execute_sql_transformation(...)`. Re-registering the same stable name and
unchanged definition returns the existing immutable version. Changing that
definition publishes the next version number. Re-executing one version with
identical parameters and input snapshot returns the existing derived dataset
only after rechecking its disposition, physical-table presence, immutable
triggers, and content hash. Retired or removed results fail with
`derived_unavailable`; drifted results fail with `derived_corrupt`.
The same centralized integrity check runs before `validate_derived(...)` and
before a published derived table can become another transformation's parent;
large-table snapshot hashing remains streaming and is reused within that call.

Use `get_dataset(..., kind="derived")` for variables, weight, lineage, and
run audit, and `validate_derived(...)` to recheck its physical relation,
contiguous row ordinals, run/version binding, and content snapshot hash.

A published materialized dataset may be used as the next run's
`parent_kind="derived"`. The logical input relation inside SQL is always
`parent`.

## SQL boundary

The implementation parses SQLite SQL with sqlglot.
It accepts one outer SELECT over only `parent`. It rejects DDL, DML,
comments, multiple statements, undeclared relations, positional parameters,
volatile functions, and functions outside its deterministic allowlist.
Scope-aware resolution prevents nested CTEs from hiding undeclared relations,
and `parent` cannot be shadowed or database-qualified. The parent CTE is merged
into an existing leading `WITH` through the AST. SQLite's database authorizer
independently permits reads only from the exact resolved physical parent.
Each outer projection is AST-checked against its declared lineage: computed
and aggregate contributors must exactly cover referenced columns, identity
must be an unchanged direct parent projection, and constants may reference no
columns. The AST also determines the required lineage kind: aggregate-function
expressions are `aggregate`, transformed non-aggregate references are
`computed`, reference-free literals are `constant`, and exact passthroughs are
`identity`. `grouping` and `ordering` roles are accepted only when the matching
source is actually grouped or ordered. `COUNT(*)` and `COUNT(1)` fail with
`aggregate_lineage_unrepresentable` because this profile's lineage model has
no relation-level contributor; column aggregates such as `COUNT(score)` are
supported with that column declared as a contributor.

The AST also determines row semantics. Plain single-parent SELECTs are
`one_to_one`, WHERE-only shapes are `filter`, joins are `join`, and aggregate,
GROUP BY, or DISTINCT shapes are `aggregate`. CTE, scalar-subquery, window,
limit/offset, and mixed join/aggregate shapes are classified `other` and stay
fail-closed unless explicitly declared as such. An omitted declaration is
filled from this classification; an explicit mismatch is rejected.

Every outer SELECT must have an ORDER BY made of declared output columns. The
SQL must explicitly declare `NULLS FIRST` or `NULLS LAST`; the executor stores
the AST-equivalent uppercase direction, NULL placement, and fixed collation.
Before publication it asks the database to prove that the evaluated order tuple
has no NULLs and no ties. `__row_ordinal` is then contiguous from one in that
total order.

Named parameters are bound through the database driver. Parameter and input-set
hashes use the profile's RFC 8785 parameter-set-v1 and input-set-v1 envelopes.
All published outputs are materialized; view mode fails closed until immutable
view inputs can be enforced. Version 0.1 of this adapter supports JSON scalar
parameters and the exact `supported-profile` server constraint, which means
SQLite `>=3.35.0,<4.0.0`. Registration and execution both check the live
`sqlite_version()` and reject any other constraint string or runtime version.
Version 0.1 supports JSON scalar
parameters in the RFC 8785 safe domain and a single parent input. Fractional
numbers are rejected until the adapter implements a typed `binary64` parameter;
booleans, strings, null, and safe-range integers are supported. See
`capability_matrix()` for the exact
machine-readable boundary.

## Hashes, atomicity, and audit

Input and output relations use the profile's
`openstatspec-relation-snapshot-v1` SHA-256 envelope. It includes the schema
hash and rows ordered by the verified reserved ordinal, with integer ordinals,
IEEE-754 binary64 bit strings, exact strings, and explicit NULL values.

A run and its input/parameter envelopes are committed as `started` before SQL
execution. SQLite foreign keys are enabled on every workflow connection. A
materialized run writes to a reserved staging table, validates it, then renames
and publishes the physical output and derived catalog rows in one transaction.
The renamed table receives INSERT, UPDATE, and DELETE rejection triggers;
catalog tables likewise have normalized, definition-validated append-only and
one-way run-transition triggers.
A failed run remains in `transformation_run` with a redacted, code/phase-only
safe
`transformation_event`, but has no `derived_dataset` row or profile-owned
physical output. `reconcile_sql_transformation_runs(...)` marks interrupted
`started` runs failed and removes only reserved `__oss_stage_` relations.

Retirement and physical removal never delete catalog history.
`retire_derived(...)` appends a `retired` event.
`remove_derived_physical_relation(...)` appends
`physical_removal_requested` before DROP and `physical_removed` only after
the relation is confirmed absent. `reconcile_derived_removals(...)` resumes
every request without a terminal event after a crash. Removal is rejected while
a later derived run depends on the relation.

## Existing 0.1.0 catalogs

The workflow catalog is an additive optional-profile migration. On first
registration the adapter:

1. verifies the existing core `catalog_identity`;
2. refuses conflicting unowned workflow relation names;
3. creates the 13 workflow relations and their independent identity; and
4. leaves every core row, imported physical table, and historical private
   `*_catalog` compatibility table unchanged.

SQLite performs this in the existing database file. The adapter validates the
exact profile identity, columns, primary keys, unique constraints, foreign
keys, checks, and normalized reflected dialect types before use. UUID-sized
strings, unbounded text, integer, bigint, boolean, and timestamp declarations
therefore cannot be silently substituted for one another. A mismatched or drifted schema
fails closed rather than attempting an implicit rewrite. Profile schema version
2 adds enforced publication, lineage, XOR, same-dataset constraints, and exact
trigger definitions.

## CLI

```text
openstatspec catalog-list --database-url sqlite:///survey.sqlite --kind core

openstatspec derive \
  --database-url sqlite:///survey.sqlite \
  --parent-dataset-id 00000000-0000-0000-0000-000000000000 \
  --name eligible_responses \
  --sql "SELECT respondent_id FROM parent ORDER BY respondent_id ASC NULLS LAST" \
  --columns-json '[{"name":"respondent_id","storage_kind":"numeric","source":"respondent_id"}]'

openstatspec validate-derived \
  --database-url sqlite:///survey.sqlite \
  --derived-dataset-id 00000000-0000-0000-0000-000000000000
```
