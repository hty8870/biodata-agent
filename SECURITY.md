# Security policy

## Supported use

BioData Agent permits an intentionally configured public, multi-user, or Internet-facing deployment. The bundled development launcher still binds to loopback by default; that is a safe default, not a prohibition on public deployment.

Public exposure is a separate security profile, not evidence that the loopback-oriented development server is production-ready. Before exposing a deployment, implement, enable, and validate authentication, authorization, request limits, upload quotas, reverse-proxy/TLS configuration, monitoring, persistent-data backup, and deployment rollback for the target environment.

## Report a vulnerability

Do not include API keys, tokens, private data, `.env` contents, browser storage, model credentials, or exploit traffic in a public issue.

Report vulnerabilities through this repository's GitHub private vulnerability reporting (Security → Advisories → Report a vulnerability). Do not open a public issue for unpatched vulnerabilities. Include only:

- affected version or commit;
- impacted route, tool, or file;
- minimal reproduction with placeholder credentials;
- expected and observed behavior;
- whether data or credentials could leave the machine;
- suggested mitigation, if known.

If private vulnerability reporting is not enabled on this repository, open a minimal public issue that states a security concern exists and requests a private contact channel, without any detail beyond that.

## Credential and endpoint rules

- Real `.env` files and API keys must never be committed, attached to CI artifacts, copied into release archives, or placed in logs.
- CI and release-candidate workflows run without real LLM credentials and with model downloads disabled.
- A request-level temporary API key may be used only for that request; it must not be persisted by the backend.
- Browser API keys remain in session memory by default. Local persistence requires both the general settings opt-in and a separate explicit API-key opt-in; legacy stored keys without that consent marker are removed on load.
- A caller-provided custom LLM endpoint must not inherit a server-side shared key. Custom endpoints require their own request-level key.
- Public remote endpoints normally require HTTPS. If a deployment must temporarily serve telemetry over plaintext HTTP (for example, before a domain and CA certificate exist), the web client refuses plaintext endpoints unless the deployment explicitly allowlists that exact host via the `biodata-telemetry-allow-insecure` meta, and only bounded, consented, heuristically redacted usage metadata is ever sent there. Feedback text remains application-layer encrypted. This is a per-deployment risk acceptance, not a general permission for new plaintext endpoints.
- The client-shipped ingest token is an abuse-filtering credential, not a secret or proof of sender identity; rotating it alone does not provide authentication. `/v1/stats` uses a separate server-only `STATS_TOKEN` that must never enter the client.
- Network errors and provider messages exposed to users must stay bounded and sanitized, must not echo credentials or raw non-JSON bodies, and successful provider responses must respect the configured safety cap.
- The bundled local service accepts only loopback Host values by default, and browser POST requests carrying an `Origin` header must also be same-origin; this blocks the local service's inbound DNS-rebinding/CSRF route. A web deployment instead accepts a fixed `BIODATA_TRUSTED_HOSTS` allowlist and enforces mandatory, invite-based account authentication; that is the public-exposure profile described above, not a general permission for arbitrary Host values.

## Release rules

Release candidates are assembled from an allowlist and must include a file-level SHA-256 manifest. `.env`, `.git`, local models, virtual environments, caches, outputs, logs, collaboration notes, and personal work material are excluded.

The current GitHub workflow only produces a verified release-candidate artifact. It does not publish a GitHub Release or deploy production. A real deployment must add protected environments, short-lived credentials, health observation, and rollback to a previously verified artifact.

The bundled desktop and development launchers intentionally run exactly one Uvicorn worker. Download jobs, desktop-shell activity and selected caches are process-local; do not increase the worker count until those states are externalized and cross-process behavior is tested.
