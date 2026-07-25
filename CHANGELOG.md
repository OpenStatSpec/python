# Changelog

All notable changes to this reference implementation are documented here.

## 0.1.0 — unreleased

First public reference implementation of the OpenStatSpec strict wide-table
SPSS profile.

### Included

- Import of unencrypted SAV and ZSAV sources into one dedicated wide SQL table
  and its metadata catalog.
- Export of supported dataset semantics to SAV and ZSAV.
- SQLite, PostgreSQL, and MySQL/MariaDB profiles, including service-backed
  PostgreSQL and MySQL CI coverage.
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
