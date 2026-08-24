# Task010A — Final Assembly Manifest Foundation

Task010A freezes metadata only. It does not render an episode or copy video
bytes. `FinalAssemblyService` derives readiness from the canonical production
records and writes an append-only `FinalAssembly` plus ordered
`FinalAssemblyItem` rows.

## Qualified source rule

For each `ProductionShot`, a source must have a matching production execution
with `SUCCEEDED` status, a supported video artifact, a latest `QC_PASS` result
for that artifact, no `REJECTED` review, an existing project-relative source
file, and matching project/job provenance. `APPROVED` reviews are valid; a
QC-pass with no review is also valid.

When multiple candidates qualify, the service uses the repository's canonical
persisted order (`created_at`, then SQLite insertion order) for executions,
artifacts, and QC results. The newest qualified candidate wins. If one or more
qualified candidates has an `APPROVED`/`ACCEPTED` review, the newest reviewed
candidate wins instead. Filesystem timestamps and filename sorting are never
used.

## Immutability and rebuilds

Freezing a DRAFT assembly inserts one item per canonical
`ProductionShot.order_index` and changes the assembly to `READY`. READY rows
cannot accept new items or transition to another status. A later retry or QC
result therefore requires a new assembly; an earlier READY manifest remains
readable with its original execution, artifact, QC, review, and relative path
identities.

Migration 010 creates only `final_assemblies` and `final_assembly_items` with
foreign keys, project/job provenance validation, unique item order, and
project/job indexes.
