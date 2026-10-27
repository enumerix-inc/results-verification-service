# ADR index

Architecture Decision Records for the results verification service.

| ADR | Title | Status |
|---|---|---|
| [ADR-001](ADR-001-manifest-as-contract.md) | Run context manifest as the system contract | Accepted |
| [ADR-002](ADR-002-experiment-per-feature.md) | One experiment per feature set | Accepted |
| [ADR-003](ADR-003-test-classification.md) | Stability vs exploratory experiment classification | Accepted |
| [ADR-004](ADR-004-polling-listener.md) | Polling over event subscription for the listener | ~~Superseded by ADR-012~~ |
| [ADR-005](ADR-005-pre-verification-gate.md) | Pre-verification gate with abort-on-failure | Accepted |
| [ADR-006](ADR-006-sqlite.md) | SQLite for the verification database | Accepted |
| [ADR-007](ADR-007-immutable-snapshots.md) | Immutable result snapshots — no backpropagation | Accepted |
| [ADR-008](ADR-008-gold-standard-versioning.md) | Experiment-level gold standard versioning | Accepted |
| [ADR-009](ADR-009-release-as-snapshot.md) | Gold standard release as optional convenience snapshot | Accepted |
| [ADR-010](ADR-010-hybrid-workflow.md) | Hybrid planning and implementation workflow | Accepted |
| [ADR-011](ADR-011-json-blob-metrics.md) | JSON blob storage for sample metrics | Accepted |
| [ADR-012](ADR-012-postgres-notify-listener.md) | PostgreSQL NOTIFY/LISTEN for event-driven listener | Accepted |
| [ADR-013](ADR-013-mock-listener.md) | Mock listener for development and testing | Accepted |
| [ADR-014](ADR-014-experiment-naming-convention.md) | Experiment folder naming convention and run_id stamping | ~~Superseded by ADR-022~~ |
| [ADR-015](ADR-015-decoupled-entry-point.md) | Decoupled entry point to support future test generator | Accepted |
| [ADR-016](ADR-016-comparison-registry.md) | Strategy pattern for extensible comparison registry | Accepted |
| [ADR-017](ADR-017-report-structure.md) | Report structure, output format, and multi-column comparisons | Accepted |
| [ADR-018](ADR-018-pipeline-health-vision.md) | Evolution toward pipeline health monitoring | Accepted — future direction, out of current OKR scope |
| [ADR-019](ADR-019-e-drive-deletion-watch.md) | E: drive deletion watch as pipeline completion signal | Accepted |
| [ADR-020](ADR-020-metafolder-deferred.md) | Metafolder timing and RnDdata access — deferred | Accepted — deferred beyond current OKR scope |
| [ADR-021](ADR-021-comparison-scope-and-aggregate-strategies.md) | Comparison registry extension: sample vs aggregate scope | Proposed — aggregate scope pulled forward by ADR-026 |
| [ADR-022](ADR-022-explicit-manifest-handoff.md) | Explicit manifest hand-off replaces run_id stamping for run correlation | Accepted |
| [ADR-023](ADR-023-github-actions-entry-point.md) | GitHub Actions as the manual regression-run entry point | Accepted |
| [ADR-024](ADR-024-windows-service-wrapper.md) | NSSM-wrapped Windows Service for the verification service | Accepted |
| [ADR-025](ADR-025-installer.md) | Inno Setup installer with bundled Python and a per-machine config | Proposed |
| [ADR-026](ADR-026-acceptance-criteria-evaluation.md) | Acceptance criteria evaluation as aggregate-scope comparisons with sidecar sample annotations | Accepted |
