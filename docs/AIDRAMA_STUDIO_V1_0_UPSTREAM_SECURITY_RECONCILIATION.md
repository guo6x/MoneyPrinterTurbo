# AIDrama Studio V1.0 Upstream Security Reconciliation

This checkpoint reconciles a narrow, reviewed set of upstream
MoneyPrinterTurbo fixes into the AIDrama Studio V1.0 release branch. It is not
a merge of upstream `main`, and it does not change the canonical AIDrama
product architecture.

## Reconciled upstream commits

| Upstream commit | Local commit | Purpose |
| --- | --- | --- |
| `4a82f8ce67724b28bc5f722a7d6470a66d05085f` | `f28d69945825f199bc1a8c668ca27140dbc50ff0` | Restrict HTTP custom-audio resolution to the current task directory while preserving an explicit trusted-local CLI opt-in. |
| `9f3050947ed5e1470665176d53855afbe7c20b13` | `58568cf9c2944ea04d4d90b7d7ab4115d4aa5a9e` | Prevent `/tasks` static-file traversal through symlinks. |
| `7003abab1a18d612f11285eeaa01e70f9729c69e` | `3474e0b0c768106bb2c616f306e88a94016bc7af` | Validate and safely persist local material uploads. |
| `5754ff999d2a61642217e8f40da5bde22d7b2932` | `aff9daf88d361a0178e9993026e85cc071ad198c` | Sanitize client request IDs before logging and error handling. |
| `28a55ddc1d3f4fc094837352ee9a74e1b264484a` | `5d9c53f70c792b76df25e3adf0e58f4177046c4d` | Enforce the configured API key for v1 API routes and task-file access while retaining local unauthenticated behavior when no key is configured. |
| `6951758b91514459464c02ef33340ea2ecd22d75` | `efc0a8071b82c547c5a75ae3c82acd5ce173bb08` | Preserve log records across Windows/mapped-drive path roots and normalize displayed separators. |

Each imported commit retains `cherry picked from commit ...` provenance. Two
small compatibility-only test commits adapt the upstream regressions to this
branch without weakening the security assertions:

- `cf850668684c860ca4aee3449195969cc735d6ba` removes a mock for an FFmpeg
  helper that does not exist in the current runtime.
- `937b0b3` verifies the current public `/docs` and `/openapi.json` routes
  instead of the unavailable `/ping` route.

## MPT core change boundary

The reconciled runtime changes are limited to:

- `app/asgi.py`
- `app/controllers/base.py`
- `app/controllers/v1/llm.py`
- `app/controllers/v1/video.py`
- `app/models/exception.py`
- `app/services/material_upload.py`
- `app/services/task.py`
- `app/utils/logging_utils.py`
- `cli.py`
- `config.example.toml`

The accompanying MPT tests and Windows CI selection were imported with the
fixes. No MPT WebUI rewrite or broad MPT architecture refactor was performed.

## Validation evidence

- Focused upstream-security and MPT regression selection: `149 passed, 3
  skipped, 11 warnings, 79 subtests passed`.
- Complete AIDrama suite after reconciliation: `313 passed, 11 warnings`.
- Worker-log regression: `test/services/test_webui_task.py` — `16 passed`.
- Complete repository suite: `944 passed, 11 skipped, 14 warnings, 4402
  subtests passed` in 188.04 seconds.
- The historical Windows path-separator failure is fixed; no baseline failure
  remains.
- New regressions: `0`.

No dependency was installed and no live or paid provider request was made for
this checkpoint.
