# Archived npm installer packages

`agent-installer/` (`@articultur/memplex-agent-installer` v0.2.0) and
`hermes-installer/` (`@articultur/memplex-hermes-installer` v0.2.0) are
**deprecated** and no longer published: the release pipeline
(`.github/workflows/release.yml`) publishes only `npm/memplex`, whose
`memplex setup` command covers both install paths.

They remain in the tree because `memplex/release.py` still validates their
`memplex` dependency version as part of the release version-set proof. Do
not add new dependents; new install surfaces belong in `npm/memplex`.
