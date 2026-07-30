# Changelog

All notable changes to this reference implementation are documented here.

## 0.2.0 — 2026-07-30

This release extends the reference adapter while keeping imported source
datasets immutable and all unsupported paths fail-closed.

### Added

- An optional SQLite-only Transformation Workflow for immutable, versioned,
  parameterized SELECT definitions; materialized derived datasets; lineage and
  weights; public catalog APIs; and CLI operations.
- An independent Dolt core SQL profile for exact product version 2.2.2, with
  service-backed coverage, conservative adapter limits, complete compensating
  cleanup, and pre-DDL rejection of unsupported values. Transformation Workflow
  support on Dolt is not claimed.

### Changed

- SAV and ZSAV validation now rewrites legacy compatible names consistently
  across type-2 records and subtype-13/subtype-14 long-name metadata.
- Transformation publication, interrupted-run reconciliation, retirement, and
  physical-removal recovery now preserve auditable state and fail closed on
  catalog or relation drift.
- Release automation builds distributions before test-only source checkouts and
  supports idempotent rebuilds of an existing release tag.

### Specification basis

- CI and release validation are pinned to OpenStatSpec specification commit
  `34141dda023d9e0217c37c232e39f436edfb0746`; no specification tag is claimed.

## 0.1.0 — 2026-07-29

First public reference implementation of the OpenStatSpec strict wide-table
SPSS profile.

### Included

- Import of unencrypted SAV and ZSAV sources into one dedicated wide SQL table
  and its metadata catalog.
- Export of supported dataset semantics to SAV and ZSAV.
- SQLite, PostgreSQL, MySQL, and MariaDB profiles, including service-backed
  PostgreSQL 17/18, MySQL 8.4/9.7, and MariaDB 11.4/11.8/12.3 CI coverage.
- Preflight checks for target profile limits, atomic imports, validation, a
  command-line interface, and machine-readable capability and loss reports.

### Important boundaries

- This release promises semantic round-trip equivalence only for supported
  features; it never promises byte-identical SAV/ZSAV output.
- The adapter requires explicit `allow_loss` consent before exporting known
  unsupported or lossy dictionary semantics.
- Multiple-response sets, variable alignment, variable sets, and custom
  attributes have explicit capability diagnostics; see the SAV profile.
- Encrypted SPSS files are not supported.
