# SAV/ZSAV profile status

The Python adapter implements a strict, source-faithful wide-table mapping.
One SAV or ZSAV dataset becomes one table; every source variable is a physical
column and __case_ordinal preserves case order. The adapter never pivots,
chunks, or creates a cell/EAV representation.

## SPSS engine

OpenStatSpec Python requires the pinned TonisOrmisson/pyspssio fork declared in
pyproject.toml as its sole SPSS engine. There is no fallback reader or writer. The engine reads unencrypted .sav and .zsav
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

## Engine capability boundary

The `openstatspec capabilities` command and the
`openstatspec.capability_matrix()` function are the machine-readable declaration
of this boundary. It records the pinned source commit and installed engine version, and the adapter stores the same identity in every import and export operation record. The matrix deliberately distinguishes a supported feature from a feature that the underlying engine cannot observe or write faithfully.

| SPSS semantic | Pinned pyspssio status | Behaviour |
| --- | --- | --- |
| File label | Supported through the pinned fork | The adapter persists it in the dataset catalog and writes it through the IBM I/O identifier-string API. |
| Very-long UTF-8 strings | Supported | The SAV and ZSAV fixture preserves a 340-byte, multi-byte UTF-8 string through the SQL catalog and back. |
| Ordered document text | Unobservable | The engine exposes file-to-file document copying, but not reading or creating document text. Import records documents-unobservable; export requires that audited loss to be accepted. |
| Print and write formats independently | Supported as raw IBM I/O tuples | The adapter stores both tuples separately and writes them without collapsing either value. |
| Variable sets | Supported through raw IBM I/O | The adapter stores source sets in the extension catalog and writes them through the raw dictionary setter. Invalid target definitions fail before data are written. |
| Legacy compatible variable names | Fail-closed on export | A compatible name that differs from the long source name is retained in the catalog, but cannot be set through the public writer. Export requires `compatible-variable-name-not-exportable`. |
| Source encoding | UTF-8 fidelity only | UTF-8 is supported. A legacy code page is retained in metadata, but export requires `source-encoding-not-preserved` because the writer has no legacy-code-page preservation contract. |
| Custom attributes with one value | Supported | File and variable scalar attributes round trip through the normalized attribute catalog. |
| Custom-attribute value arrays | Supported through raw IBM I/O | The adapter represents an array as IBM SPSS `Name[1]`, `Name[2]`, … members and reconstructs the ordered array in the normalized catalog. |

The remaining loss codes can be supplied through `allow_loss` only when a
caller intentionally accepts the documented, machine-readable loss report.

## Distribution

The required engine includes IBM I/O Module redistributables under terms that
are separate from OpenStatSpec's Apache-2.0 source licence. See
[third-party notices](../THIRD_PARTY_NOTICES.md) before distributing a bundled
application.
