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
