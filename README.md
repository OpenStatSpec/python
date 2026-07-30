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

See [the SQL transformation workflow](docs/sql-transformation-workflow.md) for
Python and CLI examples, migration behavior, hashing, atomicity, and the exact
implemented capability boundary.

## 0.2.0 support status

The adapter requires `openstatspec-pyspssio==0.5.1.post2` as its sole SPSS
engine. Its import module remains `pyspssio`; the exact source commit is recorded
in operation metadata. There is no fallback reader or writer. It supports unencrypted SAV and ZSAV import and
SAV/ZSAV export for the semantics exposed by that engine. SQLite is the local
reference path.
PostgreSQL, MySQL, MariaDB, and Dolt are each covered by separate service-backed CI
conformance checks. Dolt support is an independent core profile pinned to 2.2.2;
other Dolt versions and unknown MySQL-wire products fail closed. Use these explicit SQLAlchemy URLs:

- SQLite: `sqlite:///dataset.sqlite`
- PostgreSQL: `postgresql+psycopg://user:password@host/database`
- MySQL/MariaDB: `mysql+pymysql://user:password@host/database`
- Dolt 2.2.2: `mysql+pymysql://user:password@host/database` (detected by server identity)

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
