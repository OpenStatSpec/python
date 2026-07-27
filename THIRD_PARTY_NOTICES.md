# Third-party notices

OpenStatSpec Python source code is licensed under Apache-2.0. Its required
SPSS engine is a separate third-party distribution:

| Component | Version | Applicable terms |
| --- | --- | --- |
| pyspssio | TonisOrmisson/pyspssio pinned commit 6a0f9fa | MIT for the Python wrapper, except for its IBM I/O Module files |
| IBM I/O Modules for IBM SPSS Statistics Data Files | bundled by the pinned pyspssio fork | IBM International License Agreement for Non-Warranted Programs and the accompanying License Information / REDIST files |

pyspssio ships the IBM licence, redistribution list, and third-party notices
inside its installed distribution, normally under pyspssio/spssio/license/.
Those materials are authoritative. They are not relicensed by this repository's
Apache-2.0 licence.

## Distribution responsibility

OpenStatSpec requires the pinned pyspssio fork declared in pyproject.toml; it has no alternate SPSS engine. This
repository does not vendor, alter, or publish the IBM binaries. Anyone who
distributes an application bundle containing the installed engine must review
and comply with the IBM materials supplied with that engine. In particular, the
IBM redistribution conditions require object-code-only distribution as part of
the distributor's application, preservation of notices, restrictions on use
and onward distribution, and end-user terms that protect IBM at least as much
as the IBM agreement. They also include other conditions, including support,
indemnity, and trademark restrictions.

Do not remove the pyspssio licence directory or notices from a bundled
installation. Do not describe IBM or its trademarks as endorsing OpenStatSpec.

This is a factual third-party notice, not legal advice. A party preparing a
binary or application distribution should have its own legal review of the
exact LA_en, REDIST, and notices files installed with the selected pyspssio
release.
