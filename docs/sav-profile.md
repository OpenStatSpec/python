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

## Declared SQL profiles

SQLite is the tested reference profile. PostgreSQL and MySQL/MariaDB have
explicit preflight declarations but need live-server conformance fixtures
before they are described as tested. An import that exceeds a target's strict
single-table column limit fails before it creates a dataset.

## Explicit current boundaries

- Imports accept unencrypted `.sav` and `.zsav` sources through pyreadstat.
- Export currently writes `.sav` only. Requesting `.zsav` fails explicitly.
- The writer restores metadata supported by pyreadstat. SPSS dictionary
  constructs that pyreadstat does not expose as stable writer inputs—such as
  multiple-response sets and variable sets—are not silently claimed as
  round-tripped and require a later profile implementation.
- Encryption and byte-identical reproduction are out of scope. The contract is
  semantic equivalence of supported values, order, and dictionary metadata.