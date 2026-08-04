# OpenStatSpec Python

The reference Python implementation of the OpenStatSpec specification.

This package implements the specification; it does not define or extend it.
The normative model lives in the `OpenStatSpec/specification` repository.

## Boundaries

For each supported import, one source dataset becomes one dedicated wide SQL
table. Cases are rows and source variables are physical SQL columns. The
singular UUID-keyed tables from the specification (`dataset`, `variable`,
`operation`, `fidelity_event`, and related metadata tables) are the public
catalog contract and the only catalog representation created or read by the
adapter. Databases created with the former `*_catalog` compatibility schema
must be remediated to the normative schema before use. The adapter does not
reshape data, create EAV or long-form tables, or harmonize studies or waves.

Unsupported source features, SQL targets, or export paths fail explicitly.
There is no silent truncation, type conversion, metadata loss, or partial
import.

## Package layout

- `openstatspec.core`: pure standard concepts, validation, versions,
  capabilities, and loss reports.
- `openstatspec.sql`: database connection and wide-table/catalog operations.
- `openstatspec.spss`: SAV/ZSAV adapter boundary.
- `openstatspec.transform`: canonical plans, frontend-neutral schema concepts,
  and plan validation.
- `openstatspec.frontends.spss`: the SPSS-like syntax frontend and convenience
  execution adapter.

## Intended workflow

```python
from openstatspec import export_sav, import_sav

import_sav("responses.sav", database_url="postgresql+psycopg://user:password@server/database", dataset_id="responses-2026")
export_sav(database_url="postgresql+psycopg://user:password@server/database", dataset_id="responses-2026", destination="responses-roundtrip.sav")
```

```text
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
The core SQLite import/export profile accepts SQLite `>=3.24.0,<4.0.0`; the
optional transformation workflow deliberately has the narrower
`>=3.35.0,<4.0.0` runtime preflight. These independent tiers do not change the
server-profile matrix. Microsoft SQL Server is not supported; its future
dialect is scoped only in the specification's
[MSSQL roadmap](https://github.com/OpenStatSpec/specification/blob/main/docs/mssql-dialect-roadmap.md).

See [the SQL transformation workflow](docs/sql-transformation-workflow.md) for
Python and CLI examples, migration behavior, hashing, atomicity, and the exact
implemented capability boundary.

## SPSS-like transformation frontend

The SPSS-like frontend lowers supported `RECODE`, `VARIABLE LABELS`, and
`VALUE LABELS` syntax into a language-neutral canonical plan. The in-place
path applies it to the same logical dataset, physical wide table, and metadata
catalog without a derived dataset, copied table, snapshot, or separate
rollback/history layer. Dolt remains the sole versioning layer for Dolt-backed
edits, and the transformer never calls `DOLT_COMMIT`.

See the [dataset transformations manual](docs/transformations.md) for schema
installation, Python and CLI surfaces, database invariants, audit provenance,
package layout, and extension guidance. Stata and SAS are unimplemented
placeholders.

## Current support status

The adapter requires `openstatspec-pyspssio==0.5.1.post2` as its sole SPSS
engine. Its import module remains `pyspssio`; the exact source commit is recorded
in operation metadata. There is no fallback reader or writer. It supports unencrypted SAV and ZSAV import and
SAV/ZSAV export for the semantics exposed by that engine. SQLite is the local
reference path.
PostgreSQL, MySQL, MariaDB, and Dolt are each covered by separate service-backed CI
conformance checks. Dolt support is an independent core profile for the
canonical stable range `>=2.2.2,<2.3.0`; earlier patches, other families,
noncanonical versions, and unknown MySQL-wire products fail closed.

The supported family claims are broader than the deliberately exact CI
evidence points: PostgreSQL 17.x/18.x is exercised at 17.10/18.4, MySQL
8.4.x/9.7.x at 8.4.11/9.7.2, and MariaDB 11.4.x/11.8.x/12.3.x at
11.4.12/11.8.8/12.3.2. Each service job checks the normalized live server
version against its exact matrix entry before that run can count as evidence.
Dolt claims the conservative 2.2.x range `>=2.2.2,<2.3.0`; its full service
suite is exercised independently at exact versions 2.2.2 and 2.2.3 using
immutable container-image digests.

| Engine/profile | Runtime supported policy | Exact CI-tested versions |
| --- | --- | --- |
| SQLite core / optional workflow | Core `>=3.24.0,<4.0.0`; optional workflow `>=3.35.0,<4.0.0` | Runtime-provided SQLite on Python 3.11–3.14 runners; not a pinned server image |
| PostgreSQL | 17.x and 18.x | 17.10 and 18.4 |
| MySQL | 8.4.x and 9.7.x | 8.4.11 and 9.7.2 |
| MariaDB | 11.4.x, 11.8.x, and 12.3.x | 11.4.12, 11.8.8, and 12.3.2 |
| Dolt | 2.2.x with `>=2.2.2,<2.3.0` | 2.2.2 and 2.2.3 |

Microsoft SQL Server (MSSQL) remains roadmap-only and is not a supported
runtime profile; see the specification's [MSSQL roadmap](https://github.com/OpenStatSpec/specification/blob/main/docs/mssql-dialect-roadmap.md).

Use these explicit SQLAlchemy URLs:

- SQLite: `sqlite:///dataset.sqlite`
- PostgreSQL: `postgresql+psycopg://user:password@host/database`
- MySQL/MariaDB: `mysql+pymysql://user:password@host/database`
- Dolt `>=2.2.2,<2.3.0`: `mysql+pymysql://user:password@host/database` (detected by server identity)

The Dolt core profile supports strict wide-table import, validation, and export;
the separate Transformation Workflow is unsupported.

Run `openstatspec capabilities` before an integration to inspect the
machine-readable feature matrix. Export is deliberately strict: if known
dictionary semantics cannot be reproduced, it stops until you pass the exact
diagnostic code with `--allow-loss`. This avoids silent loss while making an
intentional lossy export auditable.

The matrix is also available to Python callers as
`openstatspec.capability_matrix()`. It distinguishes supported semantics from
unobservable and fail-closed paths; see the SAV profile for the exact
the openstatspec-pyspssio boundary.

See [the SAV profile](docs/sav-profile.md) for feature boundaries and
[release readiness](docs/release-readiness.md) for the pre-tag checklist.
Read [third-party notices](THIRD_PARTY_NOTICES.md) before distributing a
bundled application: the required engine includes IBM redistributables under
separate terms.
