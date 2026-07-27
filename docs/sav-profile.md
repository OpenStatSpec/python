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

## Engine capability boundary

The `openstatspec capabilities` command and the
`openstatspec.capability_matrix()` function are the machine-readable declaration
of this boundary. They deliberately distinguish a supported feature from a
feature that the underlying engine cannot observe or write faithfully.

| SPSS semantic | pyspssio 0.5.1 status | Behaviour |
| --- | --- | --- |
| File label and document text | Unobservable | The public engine API exposes neither. Import records `file-label-and-documents-unobservable`; export requires that audited loss to be accepted. |
| Print and write formats independently | Supported as raw IBM I/O tuples | The adapter stores both tuples separately and writes them without collapsing either value. |
| Variable sets | Fail-closed on export | If a set cannot be inspected, or an inspected set cannot be written faithfully, it is recorded and export stops unless its exact loss is accepted. |
| Legacy compatible variable names | Fail-closed on export | A compatible name that differs from the long source name is retained in the catalog, but cannot be set through the public writer. Export requires `compatible-variable-name-not-exportable`. |
| Source encoding | UTF-8 fidelity only | UTF-8 is supported. A legacy code page is retained in metadata, but export requires `source-encoding-not-preserved` because the writer has no legacy-code-page preservation contract. |
| Custom attributes with one value | Supported | File and variable scalar attributes round trip through the normalized attribute catalog. |
| Custom-attribute value arrays | Fail-closed | Ordered arrays are preserved in the SQL catalog, but pyspssio accepts only one text value per attribute name. Export stops; it does not stringify, flatten, or silently drop values. |

The first four loss codes can be supplied through `allow_loss` only when a
caller intentionally accepts the documented, machine-readable loss report.
An attribute-value array has no corresponding writer representation, so it
remains a hard fail rather than a lossy conversion option.

## Distribution

The required engine includes IBM I/O Module redistributables under terms that
are separate from OpenStatSpec's Apache-2.0 source licence. See
[third-party notices](../THIRD_PARTY_NOTICES.md) before distributing a bundled
application.
