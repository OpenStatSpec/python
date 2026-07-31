# SAV/ZSAV profile status

The Python adapter implements a strict, source-faithful wide-table mapping.
One SAV or ZSAV dataset becomes one table; every source variable is a physical
column and __case_ordinal preserves case order. The adapter never pivots,
chunks, or creates a cell/EAV representation.

## SPSS engine

OpenStatSpec Python requires `openstatspec-pyspssio==0.5.1.post2` as its sole
SPSS engine. There is no fallback reader or writer. The engine reads
unencrypted .sav and .zsav files, and writes both formats through the same implementation.

## Verified SQL profiles

SQLite has a local reference fixture. PostgreSQL, MySQL, MariaDB, and Dolt have
separate service-backed conformance checks in GitHub Actions that import,
validate, and export the supported fixture. The family claims and exact CI
points are PostgreSQL 17.x/18.x at 17.10/18.4, MySQL 8.4.x/9.7.x at
8.4.11/9.7.2, and MariaDB 11.4.x/11.8.x/12.3.x at
11.4.12/11.8.8/12.3.2. CI compares each live normalized server version with
that exact evidence point.

Dolt is an independent core profile pinned to exact server version 2.2.2 and
detected over `mysql+pymysql` by active server identity; other Dolt versions
and unknown MySQL-wire products fail closed. The core Dolt profile does not
claim support for the separate Transformation Workflow. An import that exceeds
a target's strict single-table column or row envelope fails before it creates a
dataset.

The core SQLite profile remains `>=3.24.0,<4.0.0`; only the optional
Transformation Workflow requires `>=3.35.0,<4.0.0`. Microsoft SQL Server is
not a supported profile. Its driver, SQL-difference, preflight, CI,
conformance, security, and atomicity work is future scope in the
specification's
[MSSQL dialect roadmap](https://github.com/OpenStatSpec/specification/blob/main/docs/mssql-dialect-roadmap.md).

## Fidelity contract

The adapter must preserve and validate only the source semantics that the
selected engine or the adapter's narrow, fail-closed type-6 document bridge exposes and that a conformance fixture proves. Unsupported
semantics fail explicitly; they never silently disappear. Encryption and
byte-identical reproduction are outside the contract. The contract is semantic
equivalence of supported values, order, and dictionary metadata.

## Engine capability boundary

The `openstatspec capabilities` command and the
`openstatspec.capability_matrix()` function are the machine-readable declaration
of this boundary. It records the pinned source commit and installed engine version, and the adapter stores the same identity in every import and export operation record. The matrix deliberately distinguishes a supported feature from a feature that the underlying engine cannot observe or write faithfully.

| SPSS semantic | Pinned pyspssio status | Behaviour |
| --- | --- | --- |
| File label | Supported through openstatspec-pyspssio | The adapter persists it in the dataset catalog and writes it through the IBM I/O identifier-string API. |
| Very-long UTF-8 strings | Supported | The SAV and ZSAV fixture preserves a 340-byte, multi-byte UTF-8 string through the SQL catalog and back. |
| Ordered document text | Supported through a strict type-6 dictionary bridge plus the pinned fork | Import stores normalized document rows; export creates a temporary UTF-8 SAV source and has IBM I/O copy the records into SAV or ZSAV without invalidating ZSAV dictionary offsets. |
| Print and write formats independently | Supported as raw IBM I/O tuples | The adapter stores both tuples separately and writes them without collapsing either value. |
| Variable sets | Supported through raw IBM I/O | The adapter stores source sets in the extension catalog and writes them through the raw dictionary setter. Invalid target definitions fail before data are written. |
| Legacy compatible variable names | Supported through the strict dictionary bridge | The adapter atomically rewrites and reparses type-2, subtype-13, and VLS subtype-14 names as one consistent SAV/ZSAV dictionary; malformed or duplicate subtype-14 entries fail closed. |
| Source encoding | UTF-8 by default; legacy code page with an explicit OS locale | The caller supplies legacy_locale and the writer verifies that the emitted encoding matches the stored source encoding. Without it, export fails before output creation. |
| Custom attributes with one value | Supported | File and variable scalar attributes round trip through the normalized attribute catalog. |
| Custom-attribute value arrays | Supported through raw IBM I/O | The adapter represents an array as IBM SPSS `Name[1]`, `Name[2]`, … members and reconstructs the ordered array in the normalized catalog. |

The remaining loss codes can be supplied through `allow_loss` only when a
caller intentionally accepts the documented, machine-readable loss report.

## Distribution

The required engine includes IBM I/O Module redistributables under terms that
are separate from OpenStatSpec's Apache-2.0 source licence. See
[third-party notices](../THIRD_PARTY_NOTICES.md) before distributing a bundled
application.
