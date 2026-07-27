# OpenStatSpec Python

The reference Python implementation of the OpenStatSpec specification.

This package implements the specification; it does not define or extend it.
The normative model lives in the `OpenStatSpec/specification` repository.

## Boundaries

For each supported import, one source dataset becomes one dedicated wide SQL
table. Cases are rows and source variables are physical SQL columns. Catalog
metadata is stored separately. The adapter does not reshape data, create EAV
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

## 0.1.0 support status

The adapter requires pyspssio 0.5.1 as its sole SPSS engine. There is no
fallback reader or writer. It supports unencrypted SAV and ZSAV import and
SAV/ZSAV export for the semantics exposed by that engine. SQLite is the local
reference path.
PostgreSQL, MySQL, and MariaDB are each covered by separate service-backed CI
conformance checks. Use these explicit SQLAlchemy URLs:

- SQLite: `sqlite:///dataset.sqlite`
- PostgreSQL: `postgresql+psycopg://user:password@host/database`
- MySQL/MariaDB: `mysql+pymysql://user:password@host/database`

Run `openstatspec capabilities` before an integration to inspect the
machine-readable feature matrix. Export is deliberately strict: if known
dictionary semantics cannot be reproduced, it stops until you pass the exact
diagnostic code with `--allow-loss`. This avoids silent loss while making an
intentional lossy export auditable.

See [the SAV profile](docs/sav-profile.md) for feature boundaries and
[release readiness](docs/release-readiness.md) for the pre-tag checklist.
Read [third-party notices](THIRD_PARTY_NOTICES.md) before distributing a
bundled application: the required engine includes IBM redistributables under
separate terms.
