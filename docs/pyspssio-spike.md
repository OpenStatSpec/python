# `pyspssio` engine spike

**Status:** investigated only; not a runtime dependency and not selected by the
public adapter.

This is a bounded technical spike for evaluating
[`pyspssio`](https://pypi.org/project/pyspssio/) 0.5.1 as a future SPSS
engine. The existing `pyreadstat` engine remains unchanged.

## Environment and runtime result

The probe used Python 3.12 and `pyspssio==0.5.1` in a local development
virtual environment. The wheel includes IBM SPSS I/O Modules 28.0.1.0 native
libraries for Linux, macOS and Windows. No installed IBM SPSS Statistics
application was needed for the probe.

A local round trip created both a `.sav` and a `.zsav`, then read each
back. The following metadata survived in both files:

- UTF-8 encoding, variable names and storage widths;
- variable and value labels;
- numeric user-missing values;
- measurement levels, roles, alignments and display widths;
- file attributes and variable attributes;
- case-weight variable;
- a dichotomous multiple-response set.

The public API used was `read_metadata()`, `read_sav()` and
`write_sav()`; it supports both `.sav` and `.zsav` according to the
suffix.

## Material limitations found

This is not yet a complete-fidelity engine.

1. **Variable sets did not round trip.** A `var_sets` definition supplied to
   `write_sav()` was absent from the metadata read back. The upstream source
   itself notes a dictionary-commit / 8-byte-compatible-name limitation for
   variable sets.
2. **Print and write formats are not independently represented by the public
   API.** `Header.var_formats` reads the print format; its setter calls both
   `spssSetVarPrintFormat` and `spssSetVarWriteFormat` with the same value.
   An OpenStatSpec exporter must retain and restore the two formats separately.
3. **No public document-text or file-label property was found** in
   `Header`. These must be added upstream or accessed through a supported
   lower-level binding before this engine can satisfy the full profile.
4. The functional probe covers one modern Unicode file. It does not establish
   compatibility for legacy code-page SAV files, long-string edge cases, or
   arbitrary pre-existing SPSS files.

## License and CI boundary

The Python wrapper is MIT, but its wheel contains IBM I/O Modules under a
separate IBM licence. In particular, the accompanying redistribution terms
apply to the bundled native libraries and are not covered by this repository's
Apache-2.0 licence.

Therefore this spike intentionally:

- does **not** add `pyspssio` to project dependencies;
- does **not** run it in GitHub Actions; and
- does **not** change adapter capability claims.

Adopting it requires a maintainer licence/distribution review and an explicit
optional-engine packaging decision. CI can then test it only on platforms and
under terms approved for installing the bundled IBM modules.

## Next implementation slice

If approved after that review, add a private `PyspssioEngine` behind the
existing engine boundary, initially with a hermetic SAV/ZSAV metadata fixture.
Do not make it default until the implementation proves or deliberately reports
each of these separately: file label/documents, variable sets, independent
print/write formats, custom attributes, multiple-response sets, roles,
alignment, display width and legacy encodings.
