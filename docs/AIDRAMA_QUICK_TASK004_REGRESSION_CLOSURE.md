# Task004 Regression Closure — material cache

## Original failure

The full Task004 regression run reported
`test/services/test_material_cache.py::TestMaterialSearchCache::test_corrupted_cache_is_removed_without_breaking_search` with `warning.called == False`, while the cache file was removed successfully. The unrelated Windows worker-log path assertion remains the pre-existing baseline.

## Investigation and root cause

An exact diff audit of `d2bf6103..4c5aec32` found no changes under `app/`, `services/`, material cache, storage, or logger implementation; Task004 changes were confined to AIDrama modules and tests. The cache loader has two invalid-cache paths: malformed payloads log a warning, while stale/future-mtime entries were removed silently. Under the full-suite ordering and Windows timestamp behavior, the corrupted fixture could enter the stale invalidation branch, producing the observed assertion failure despite correct deletion.

`REGRESSION_ROOT_CAUSE=stale/invalid cache cleanup did not emit an observable warning before deletion; full-suite ordering exposed this branch. No Task004 domain code or MPT API behavior was involved.`

## Minimal correction

`app/services/material_cache.py` now emits a warning immediately before deleting an expired or future-dated cache. Cache invalidation, deletion, return value, and remote-search fallback are unchanged. This preserves existing behavior while making all invalid-cache cleanup observable and deterministic.

## Validation

- Python compile: PASS
- `git diff --check`: PASS
- `test/aidrama_studio/`: 24 passed
- Material cache module: 19 passed
- Exact corrupted-cache node: PASS
- Full project: 622 passed, 1 failed, 10 skipped; the only failure is the known Windows worker-log path-separator baseline.
- No dependency installed; no Task005 or Reference Asset Center work started.
