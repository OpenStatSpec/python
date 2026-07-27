# SAV/ZSAV profile status

The Python adapter implements a strict, source-faithful wide-table mapping.
One SAV or ZSAV dataset becomes one table; every source variable is a physical
column and __case_ordinal preserves case order. The adapter never pivots,
chunks, or creates a cell/EAV representation.

## SPSS engine

OpenStatSpec Python requires pyspssio 0.5.1 as its sole SPSS engine. There is
no fallback reader or writer. The engine reads unencrypted .sav and .zsav
files, and writes both formats through the same implementation.

## Verified SQL profiles

SQLite has a local reference fixture. PostgreSQL, MySQL, and MariaDB have
separate service-backed conformance checks in GitHub Actions that import,
validate, and export the supported fixture. MySQL 8.4 and MariaDB 11.4 exercise
the shared profile contract; this does not claim coverage for every server
configuration. An import that exceeds a target's strict single-table column
limit fails before it creates a dataset.

## Fidelity contract

The adapter must preserve and validate only the source semantics that the
selected engine exposes and that a conformance fixture proves. Unsupported
semantics fail explicitly; they never silently disappear. Encryption and
byte-identical reproduction are outside the contract. The contract is semantic
equivalence of supported values, order, and dictionary metadata.

## Distribution

The required engine includes IBM I/O Module redistributables under terms that
are separate from OpenStatSpec's Apache-2.0 source licence. See
[third-party notices](../THIRD_PARTY_NOTICES.md) before distributing a bundled
application.
