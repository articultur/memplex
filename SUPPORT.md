# Support

Start with the [canonical real-value CLI guide](docs/guides/real-value-cli.md) for exercised CLI
workflows and their evidence limits. Search the repository's existing
[GitHub Issues](https://github.com/articultur/memplex/issues) for a matching report.

Issues is enabled. Use the [support request form](https://github.com/articultur/memplex/issues/new?template=support_request.yml)
for usage and environment help, or the
[bug report form](https://github.com/articultur/memplex/issues/new?template=bug_report.yml) for a
reproducible defect. GitHub Discussions is not enabled and is not a support channel. Do not use
unrelated pull requests or private security reports for general support.

Public project conduct reports use the
[conduct report form](https://github.com/articultur/memplex/issues/new?template=conduct_report.yml),
not a support or bug form. No project-controlled confidential conduct intake is currently
available. Do not put confidential conduct details into a public support request.

## Diagnostic checklist

The Issue Forms require:

- Memplex version, install method, operating system, Python version, and relevant optional extras.
- The exact argv shape and exit status, with secret values redacted.
- Storage backend and whether the run is Lite local, loopback sync, temporary PostgreSQL, or a
  long-lived deployment.
- Minimal reproduction steps, expected behavior, observed behavior, and fresh test counts.
- For agent integration, the agent name and non-sensitive identity/config shape; state separately
  whether real-host G008 readiness was verified.
- For PostgreSQL, whether pgvector, `pg_dump`, and `pg_restore` are available, without posting the
  DSN.

## Redaction

Redact passwords, access tokens, principal tokens, signing keys, HMAC keys, URI userinfo, query
credentials, session/user identifiers, private memory, and PostgreSQL DSNs. Do not attach backup
artifacts or paste raw stdout/stderr until you have checked every line. Preserve non-sensitive
command names, flag names, status codes, and stream lengths so the failure remains diagnosable.

Suspected vulnerabilities, credential exposure, or privacy failures belong in the private
[security reporting channel](SECURITY.md), not the public support index.
