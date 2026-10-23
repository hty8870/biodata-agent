# Telemetry Receiver

This directory contains a standalone receiver for telemetry packages that have
already been sanitized by the client. It validates the package contract,
applies server-side defensive sanitization, enforces quotas, and persists
accepted records for later analysis.

## Contract

- `POST /v1/ingest` accepts the `biodata-telemetry/1` package schema.
- `GET /v1/stats` returns service counters and the latest cleanup summary.
- `GET /healthz` reports application and storage connectivity.
- The receiver rejects unknown top-level fields, applies bounded request and
  event limits, and treats repeated packets and events idempotently.
- `server_hint` is additive. Clients that do not consume it continue to work.

Before storage, the service removes sensitive keys, reduces endpoint values to
a host representation, masks sensitive free text, and keeps only sanitized
content out of logs and responses. Feedback records are optional and require
configured decryption material; other telemetry records do not depend on it.

## Configuration

Copy `.env.example` to an operator-managed secret store or local environment
file. `INGEST_TOKEN`, `STATS_TOKEN`, and `DATABASE_URL` are required.
Optional settings control request limits, quota limits, browser origins, worker
capacity, data lifecycle policy, and export location. Do not place real values
in the repository, image, client bundle, or command history.

## Deployment Template

Use the included container files as a template and replace every operator-owned
placeholder:

- `<service-host>` and `<service-port>` for listener and ingress mapping.
- `<public-origin>` for allowed browser origins.
- `<storage-url>` and `<persistent-data-dir>` for durable storage.
- `<export-dir>`, `<retention-policy>`, and `<cleanup-schedule>` for
  artifact and lifecycle management.

Place the service behind the operator's TLS and network controls. Restrict
browser origins to the public UI origin and keep administrative credentials
server-side. Capacity changes require a matching storage and rate-limit plan.

Network boundary in this template: the host port mapping binds
`127.0.0.1:8471` only, so the receiver is reachable from the host loopback
where the operator's TLS reverse proxy terminates public traffic. The
application inside the container still listens on `0.0.0.0:8471`, which is
container-network reachability, not a public listener. An operator who needs a
different ingress (direct binding, load balancer, private network) must replace
the port mapping deliberately and keep the TLS origin and origin allowlist in
sync with that decision.

## Operations

The application records the result of its configured cleanup policy for
`/v1/stats`. The helper under `scripts/` supports a manual dry run with the
active environment settings. Use `scripts/telemetry_export.py` for read-only
exports and `scripts/telemetry_delete.py` for explicit deletion requests.

## Development

Tests inject an in-memory storage backend through the same SQLAlchemy
abstraction. Run the receiver test suite with the project Python environment.

## Boundaries

- Quotas reduce abuse; they are not sender authentication.
- Horizontal scaling requires shared rate-limit and quota state.
- The receiver does not trust client-supplied forwarding headers.
- The telemetry contract is versioned; schema changes need explicit
  compatibility behavior.
