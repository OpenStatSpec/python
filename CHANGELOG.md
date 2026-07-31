# Changelog

All notable changes to this reference implementation are documented here.

## 0.4.0 — 2026-07-31

### Added

- Added a public canonical transformation-plan in-place API accepting either
  typed plan objects or strictly validated mappings.
- Added apply-plan and install-in-place-schema CLI workflows alongside the
  compatible apply-spss command.
- Added explicit audit provenance distinguishing canonical plan documents from
  SPSS syntax sources.
- Added a transformation manual covering the canonical core, frontend boundary,
  execution invariants, CLI/API workflows, and extension rules.

### Changed

- Moved the SPSS parser, binder, compiler, and convenience execution adapter to
  the dedicated openstatspec.frontends.spss package.
- Kept openstatspec.transform language-neutral with canonical plan, schema, and
  live-schema validation modules while preserving previous SPSS import paths.
- Reserved empty Stata and SAS frontend directories without exposing parsers,
  capabilities, CLI choices, or support claims.
- Strengthened pre-mutation target-type checks and target-scoped physical table
  identity guards. The executor continues to modify the same dataset and table
  without creating OpenStatSpec rollback, copy, snapshot, or history layers.

### Specification basis

- Conformance and release validation continue to use OpenStatSpec specification
  release v0.2.0 at exact commit
  79339ec3d8f8aa81789b7e85f6b8afa6f1374e50.

## 0.3.0 — 2026-07-31

### Added

- Added a bounded SPSS-like `RECODE`, `VARIABLE LABELS`, and `VALUE LABELS`
  parser with stable diagnostics and conformance fixtures.
- Added canonical Transformation Plan serialization, RFC 8785 hashes, schema
  binding, deterministic typed operations, and an agent-facing CLI/API.
- Added product-neutral in-place execution with same dataset/table identity,
  direct data and metadata mutation, and compact operation audit. Dolt adds
  expected branch/HEAD and clean-working-set checks.

### Changed

- Version history, diff, rollback, restoration, and commit remain database
  responsibilities; the transformer creates no derived/copy/snapshot/recovery
  layer and performs no `DOLT_COMMIT`.
- SQL server support now distinguishes conservative family claims from exact
  CI evidence: PostgreSQL 17.x/18.x at 17.10/18.4, MySQL 8.4.x/9.7.x at
  8.4.11/9.7.2, and MariaDB 11.4.x/11.8.x/12.3.x at
  11.4.12/11.8.8/12.3.2. Each service job verifies the live normalized version.
- Dolt remains an independent core profile, now claiming canonical stable
  versions `>=2.2.2,<2.3.0` and running its complete service suite at exact
  versions 2.2.2 and 2.2.3 from immutable container-image pins.
  SQLite retains its core `>=3.24.0,<4.0.0` and optional workflow
  `>=3.35.0,<4.0.0` tiers. MSSQL remains unsupported and roadmap-only.

### Specification basis

- CI, release validation, and machine-readable capabilities use OpenStatSpec
  specification release `v0.2.0` at exact commit
  `79339ec3d8f8aa81789b7e85f6b8afa6f1374e50`.

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

- CI and release validation are pinned to OpenStatSpec specification release
  `v1.0.0-rc.1` at exact commit
  `fef0dc6f4b17ff7141dad3f49d0524c63efbfed5`.

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
