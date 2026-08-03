# OpenStatSpec Python

The reference Python implementation of the OpenStatSpec specification.

This package implements the specification; it does not define or extend it.
The normative model lives in the `OpenStatSpec/specification` repository.

## Boundaries

For each supported import, one source dataset becomes one dedicated wide SQL
table. Cases are rows and source variables are physical SQL columns. The
singular UUID-keyed tables from the specification (`dataset`, `variable`,
`operation`, `fidelity_event`, and related metadata tables) are the public
catalog contract. Historical `*_catalog` tables are an internal compatibility
layer for the current exporter and are not the standard database interface.
The adapter does not reshape data, create EAV
or long-form tables, or harmonize studies or waves.

Unsupported source features, SQL targets, or export paths fail explicitly.
There is no silent truncation, type conversion, metadata loss, or partial
import.

## Package layout

- `openstatspec.core`: pure standard concepts, validation, versions,
  capabilities, and loss reports.
- `openstatspec.sql`: database connection and wide-table/catalog operations.
- `openstatspec.spss`: SAV/ZSAV adapter boundary.

## Intended workflow

```python
from openstatspec import export_sav, import_sav, initialize_catalog

database_url = "postgresql+psycopg://user:password@server/database"
initialize_catalog(database_url=database_url)
import_sav("responses.sav", database_url=database_url, dataset_id="responses-2026")
export_sav(database_url="postgresql+psycopg://user:password@server/database", dataset_id="responses-2026", destination="responses-roundtrip.sav")
```

```text
openstatspec init --database-url postgresql+psycopg://...
openstatspec import responses.sav --database-url postgresql+psycopg://... --dataset-id responses-2026
openstatspec export --database-url postgresql+psycopg://... --dataset-id responses-2026 --output responses-roundtrip.sav
```

## Optional database-first SQL workflow

Imported datasets remain immutable source records. The optional SQL
transformation profile can register versioned, parameterized SQLite SELECT queries,
materialize results, record lineage and weights, and expose
derived datasets through a public catalog API. It uses a separate profile
catalog and never presents SQL output as an imported source dataset.
Workflow operations support SQLite only in this milestone and fail closed on
PostgreSQL/MySQL/MariaDB; core import/export database support is unchanged.

See [the SQL transformation workflow](docs/sql-transformation-workflow.md) for
Python and CLI examples, migration behavior, hashing, atomicity, and the exact
implemented capability boundary.

## 0.1.0 support status

The adapter requires `openstatspec-pyspssio==0.5.1.post2` as its sole SPSS
engine. Its import module remains `pyspssio`; the exact source commit is recorded
in operation metadata. There is no fallback reader or writer. It supports unencrypted SAV and ZSAV import and
SAV/ZSAV export for the semantics exposed by that engine. SQLite is the local
reference path.
PostgreSQL, MySQL, and MariaDB are each covered by separate service-backed CI
conformance checks. Use these explicit SQLAlchemy URLs:

- SQLite: `sqlite:///dataset.sqlite`
- PostgreSQL: `postgresql+psycopg://user:password@host/database`
- MySQL/MariaDB/Dolt wire protocol: `mysql+pymysql://user:password@host/database`

Catalog creation and additive migration are explicit: run `initialize_catalog`
or `openstatspec init` before import, read, validation, or export. Those
operations fail closed on absent, foreign, ambiguous, unverified, or
migration-required catalogs and never auto-create catalog relations. Failure
cleanup uses compensating actions where the server does not provide atomic DDL;
a cleanup failure produces machine-readable residual inventory and a best-effort
failed-operation audit in an otherwise verified catalog.

Dolt identity requires an exact `Dolt` version comment, non-empty exact
`DOLT_VERSION()`, and an explicit active branch. Dolt writes load the
`openstatspec-specification` companion distribution through
`DoltConformanceSource.packaged()` by default. The packaged concrete
declaration directory is intentionally empty, so every operational Dolt write
path currently fails before mutation. There are no mirrored Python evidence
maps or validator rules.

Tests and explicitly configured local integrations may inject
`DoltConformanceSource.from_directory(specification_root)`. The same shared
validator must then find exactly one concrete declaration matching the active
Dolt product version, `openstatspec-python`, exact adapter version, and pinned
specification commit. Missing, invalid, empty, or ambiguous sources all fail
closed. The proposed 305-source-variable/306-physical-column envelope is not a
Dolt server limit. The read-only `dolt_state_snapshot()` and
`openstatspec dolt-state` remain available for the bound database/branch
working set, HEAD, status, and three diff summaries with deterministic digests.
Core OpenStatSpec never runs `DOLT_ADD`, `DOLT_COMMIT`, checkout, reset, or
branch-changing operations.

Run `openstatspec capabilities` before an integration to inspect the
machine-readable feature matrix. Export is deliberately strict: if known
dictionary semantics cannot be reproduced, it stops until you pass the exact
diagnostic code with `--allow-loss`. This avoids silent loss while making an
intentional lossy export auditable.

Filesystem publication and SQL audit finalization cannot form one atomic 2PC
transaction. Export therefore records a running operation, destination, and
unique durable prior-file backup path before publication; it retains that
backup until the SQL operation reaches `succeeded`. A publication or
finalization failure restores the prior destination and closes the operation
as `failed`. Failure to remove the backup after success never rewrites the
successful operation: it raises `backup_retained` with the durable path and
appends a warning best-effort, so a confidential duplicate is discoverable.

The matrix is also available to Python callers as
`openstatspec.capability_matrix()`. It distinguishes supported semantics from
unobservable and fail-closed paths; see the SAV profile for the exact
the openstatspec-pyspssio boundary.

See [the SAV profile](docs/sav-profile.md) for feature boundaries and
[release readiness](docs/release-readiness.md) for the pre-tag checklist.
Read [third-party notices](THIRD_PARTY_NOTICES.md) before distributing a
bundled application: the required engine includes IBM redistributables under
separate terms.
