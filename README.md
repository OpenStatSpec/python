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

The initial scaffold deliberately declares no conforming SPSS or SQL profile.
The entry points are stable, but fail explicitly until an implementation has
been proven by OpenStatSpec conformance fixtures.
