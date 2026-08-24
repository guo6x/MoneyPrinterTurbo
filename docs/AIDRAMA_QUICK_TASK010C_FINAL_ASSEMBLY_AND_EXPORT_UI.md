# AIDrama Quick Task010C — Final Assembly and Export UI

The `后期与成片` page now exposes the existing Task010A/010B backend through
a product-facing flow:

1. 成片准备度 — total, eligible, blocked shots, and estimated duration.
2. 生成成片 — creates/freezes a manifest and invokes the real synchronous
   runtime service.
3. 成片预览 — previews only a persisted successful MP4 resolved through the
   project-scoped runtime boundary.
4. 导出 MP4 — downloads that same validated file without transcoding.
5. 成片历史 — shows attempts and allows historical successful versions to be
   selected.

Readiness, manifest freezing, canonical ordering, source validation, retry
semantics, output probing, SHA-256 calculation, and path safety remain in
their canonical services. The page performs no SQL and never displays raw
absolute filesystem paths. Failed attempts show sanitized reasons; retry uses
the same immutable manifest. A new assembly version is offered only as an
explicit secondary action when current production outputs should be frozen
again.

The page deliberately contains no subtitle, TTS, BGM, transition, title,
watermark, AI-editing, QC, desktop, or provider controls. Task010C adds no
migration and no dependencies.
