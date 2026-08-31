# Governance

This document defines ownership and escalation for Memplex. User-facing behavior claims follow
the [canonical real-value CLI guide](docs/guides/real-value-cli.md) and its evidence boundaries.

## Roles

- **Repository owner:** the `articultur` GitHub organization has final responsibility for access,
  releases, security advisories, policy enforcement, and maintainer appointment.
- **Maintainers:** collaborators granted write or review authority by the repository owner. They
  triage changes, enforce gates, review evidence, and protect architecture contracts.
- **Contributors:** people proposing code, tests, documentation, or review findings through the
  repository workflow. Contribution does not imply merge or release authority.
- **Users:** people evaluating or operating Memplex who provide reproducible reports and respect
  security and privacy boundaries.

## Decisions

Routine changes are decided through review based on correctness, scope, architecture fit, test
evidence, security, and maintainability. The repository owner or an authorized maintainer merges
accepted changes. Architecture, compatibility, release, or policy decisions require an explicit
written rationale in a pull request, issue, or architecture decision record. Silence is not
consent, and a passing subset of tests is not proof that a required gate passed.

Security embargoes and urgent credential-exposure containment may be handled privately first.
The public record is updated when disclosure is safe.

## Escalation

Start technical disagreements in the relevant review with competing evidence, impact, and the
smallest reversible option. If unresolved, open the enabled
[governance proposal Issue Form](https://github.com/articultur/memplex/issues/new?template=governance_proposal.yml).
A maintainer decides routine disputes; repository-wide architecture, release, security,
governance, and enforcement appeals go to the `articultur` repository owner.

Public conduct and enforcement reports use the
[conduct report Issue Form](https://github.com/articultur/memplex/issues/new?template=conduct_report.yml).
No project-controlled confidential conduct intake is currently available. Do not put confidential
conduct details into a governance proposal or another public issue. Suspected vulnerabilities
follow [SECURITY.md](SECURITY.md), whose private Advisory intake is vulnerability-only and is not a
conduct appeal path. The repository has no GitHub Discussions forum, so this policy does not route
decisions there.
