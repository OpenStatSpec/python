# pyspssio engine assessment

**Status:** selected as the required, sole Python SPSS engine.

OpenStatSpec Python requires `openstatspec-pyspssio==0.5.1.post2`, built from
TonisOrmisson/pyspssio commit `e069adf`, for both SAV and ZSAV import and
export. There is no alternate SPSS engine.

## Probe result

A local round trip created both .sav and .zsav, then read each back. The
following metadata survived in the probe:

- UTF-8 encoding, variable names and storage widths;
- variable and value labels;
- numeric user-missing values;
- measurement levels, roles, alignments and display widths;
- file attributes and variable attributes;
- case-weight variable;
- a dichotomous multiple-response set.

The public API used was read_metadata(), read_sav(), and write_sav().

## Required conformance work

This engine selection does not relax the OpenStatSpec profile. Every claimed
feature must have a hermetic round-trip fixture. Current engine/API questions
that require explicit tests or upstream work include:

1. variable-set round trips;
2. independent print and write formats;
3. ordered document text;
4. legacy code-page files and unusual very-long-string boundaries beyond the covered 340-byte UTF-8 fixture.

Until a feature is proved, the adapter must report it as unsupported rather
than silently losing it.

## Licence boundary

The pyspssio wrapper is MIT, except for IBM I/O Module files bundled in its
wheel. Those redistributables have their own IBM terms. pyspssio is a required
runtime dependency, not an optional plugin; OpenStatSpec does not copy or
modify its IBM binaries. See [third-party notices](../THIRD_PARTY_NOTICES.md)
for the distribution boundary and the location of the authoritative installed
terms.
