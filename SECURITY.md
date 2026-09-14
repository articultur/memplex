# Security policy

Use the [canonical real-value CLI guide](docs/guides/real-value-cli.md) for tested command forms
and the limits of local, agent, sync, and backup evidence.

## Known advisories in optional dependencies

The optional `vector` extra (`pip install memplex[vector]`) pulls in
[ChromaDB](https://pypi.org/project/chromadb/). As of 2026-09-14 every
released ChromaDB version from 0.4.17 through 1.5.9 (the latest) falls
inside at least one published advisory with **no fixed upstream release
yet**:

| GHSA | Severity | Summary |
| --- | --- | --- |
| [GHSA-f4j7-r4q5-qw2c](https://github.com/advisories/GHSA-f4j7-r4q5-qw2c) | critical | Pre-authentication code injection (≥ 1.0.0) |
| [GHSA-36p7-vc44-83pf](https://github.com/advisories/GHSA-36p7-vc44-83pf) | critical | Code injection (≥ 0.4.17) |
| [GHSA-2wm9-hf6c-p5cr](https://github.com/advisories/GHSA-2wm9-hf6c-p5cr) | high | Cross-tenant data access (≥ 0.4.17) |
| [GHSA-xph7-9rjv-w5fr](https://github.com/advisories/GHSA-xph7-9rjv-w5fr) | high | RBAC not scoped to tenant/database/collection (≥ 0.5.0) |

Scope and mitigation:

- Memplex's default deployments — the Lite store with its built-in
  vector index, and the PostgreSQL + pgvector path — **never load
  chromadb**. The package is only reachable when user code explicitly
  requests the `chroma`/`auto` vector-store backend from
  `memplex.storage.vector`.
- `create_vector_store("chroma")` **fails closed** (raises) while the
  installed chromadb is inside a known-unpatched range, and
  `create_vector_store("auto")` degrades to the dependency-free
  InMemory backend with an error log. Both gates lift automatically for
  chromadb versions outside every published range.
- To explicitly accept the risk on an isolated network, pass
  `allow_vulnerable_chroma=True` or set
  `MEMPLEX_ALLOW_VULNERABLE_CHROMA=1`. Do not expose a chroma-backed
  deployment to untrusted networks while the advisories are open.
- The literal advisory ranges live in
  [`memplex/storage/vector.py`](memplex/storage/vector.py)
  (`_CHROMA_ADVISORIES`); this section and that table are updated
  together.

## Reporting a vulnerability

Report vulnerabilities privately through the repository's vulnerability-only GitHub Security
Advisory form:

https://github.com/articultur/memplex/security/advisories/new

Do not open a public issue for a suspected vulnerability. Include affected versions, a concise
impact statement, reproduction steps, and the smallest safe evidence needed to confirm the
problem. GitHub authentication is required to use the private advisory form.
This intake is only for suspected vulnerabilities. It is not a conduct-reporting or project
enforcement channel.

## Sensitive material

Never submit live credentials, principal tokens, signing keys, HMAC keys, private memory content,
or complete PostgreSQL DSNs. Replace each secret with a stable redaction marker while preserving
the command shape and failure status.

## HTTP authentication: shared secrets vs. principal registry

The legacy `MEMPLEX_API_KEY` / `MEMPLEX_BEARER_TOKEN` environment variables are a development
convenience only. Every caller presenting the shared secret is granted the same
`local_development_context` identity, so there is no tenant isolation between holders of the
same secret, no per-principal revocation, and no audit attribution. The server logs a one-time
startup warning when this mode is active.

For any multi-tenant or non-local deployment, configure the principal token registry
(`MEMPLEX_PRINCIPALS_JSON`) instead: each principal authenticates with its own token and
receives an isolated tenant-scoped authorization context. The `production` deployment profile
refuses to start with shared-secret authentication. Credential-validation failures are counted
in-process (`auth_failures_total`, surfaced on the authenticated `/health` endpoint) and logged
with the peer IP only — never with the presented credential.


A backup artifact may contain user memory, identifiers, schema metadata, and integrity material.
Do not attach a real backup artifact to an issue or pull request. Reproduce with disposable data,
store artifacts outside the repository, restrict access, and delete them when the investigation
ends. Share any necessary sensitive artifact only inside the private Security Advisory after
agreeing on a transfer method there.

## Disclosure and response

The `articultur/memplex` repository owner coordinates validation, remediation, release decisions,
and disclosure through the advisory. No response-time guarantee is promised. Public disclosure
must wait until a fix or documented mitigation is available and affected users can act safely.
