# Architecture

`openstatspec.core` contains only pure concepts defined by the standard:
versioning, validation concepts, capability declarations, and loss reports.
`openstatspec.sql` owns database-target and strict wide-table/catalog work.
`openstatspec.spss` owns SAV/ZSAV source and export behavior.

The public workflow remains database-connected. A supported import receives an
SPSS source, a database URL or connection, and a dataset identity. It creates
one dedicated wide data table plus separate catalog metadata. Export identifies
the database dataset and writes a supported SPSS file.

No layer may silently substitute EAV, long-form views, JSON, pivots, automatic
harmonization, truncation, or partial import for the standard's strict mapping.


## Optional SQL transformation profile

`openstatspec.sql.workflow` layers a separate, independently identified
catalog beside the core contract. It owns immutable transformation
definitions/versions, driver-bound runs, input snapshots, derived relations,
variables, lineage, weights, safe events, and append-only disposition events.
Its executable backend is SQLite only: foreign keys, transactional staging,
scope-aware AST validation, and the SQLite authorizer form one tested security
and atomicity boundary. Other core database profiles do not imply workflow
support.
It may read validated core or published derived datasets but never writes a SQL
result into the source-faithful core `dataset` relation. Read-only discovery
is provided by `openstatspec.sql.catalog_api`; historical `*_catalog`
relations remain private compatibility state.
