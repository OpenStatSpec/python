# SAV/ZSAV profile status

The Python adapter implements a strict, source-faithful wide-table mapping.
One SAV or ZSAV dataset becomes one table; every source variable is a physical
column and `__case_ordinal` preserves case order. The adapter never pivots,
chunks, or creates a cell/EAV representation.

## Verified SQLite path

The SQLite reference path has a round-trip fixture covering numeric and string
values, system-missing numerics, blank strings, labels, value labels, formats,
measurement level, user-missing rules, file notes, and source encoding
metadata. User-missing values remain stored values; their interpretation is
kept as metadata and is restored to SAV on export.

## Verified SQL profiles

SQLite has a local reference fixture. PostgreSQL and MySQL/MariaDB have
service-backed conformance checks in GitHub Actions that import, validate, and
export the same supported fixture. The MySQL service test verifies the shared
MySQL/MariaDB profile contract; it does not claim separately tested coverage
for every MariaDB release or server configuration. An import that exceeds a
target's strict single-table column limit fails before it creates a dataset.

## Explicit current boundaries

- Imports accept unencrypted `.sav` and `.zsav` sources through pyreadstat.
- Exports write SAV and ZSAV; ZSAV output uses the engine's compressed
  SAV writer path.
- The writer restores metadata supported by pyreadstat. SPSS dictionary
  constructs that pyreadstat does not expose as stable writer inputs—such as
  multiple-response sets and variable sets—are not silently claimed as
  round-tripped and require a later profile implementation.
- Encryption and byte-identical reproduction are out of scope. The contract is
  semantic equivalence of supported values, order, and dictionary metadata.