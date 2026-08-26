# AIDrama V1 upstream delta reconciliation — 2026-08-26

This checkpoint audits the upstream MoneyPrinterTurbo changes after the
coordinated baseline `6951758b91514459464c02ef33340ea2ecd22d75` and keeps the
AIDrama V1 boundary intact. It is not an upstream merge.

## Topology and audit scope

- Repository: `guo6x/MoneyPrinterTurbo`
- Branch base: `18a2b1103fcb59264ee1aee5a6225da26cc59d7e`
- Upstream: `https://github.com/harry0703/MoneyPrinterTurbo.git`
- Audited upstream HEAD: `465b8b35ef158b49d3b30712599d98b026c2cc66`
- Range: `6951758..upstream/main` (14 commits, including three merge commits)

## Commit classification

| Commit | Classification | Reason |
| --- | --- | --- |
| `b3599f63` | `DEFER_NOT_RELEVANT` | CLI batch-manifest feature; outside AIDrama and explicitly out of scope. |
| `50091bdd` | `DEFER_NOT_RELEVANT` | CLI preflight side-effect change; no AIDrama or fallback impact. |
| `68ce6523` | `DEFER_NOT_RELEVANT` | README/resource-only update. |
| `0bfb0bca` | `DEFER_NOT_RELEVANT` | CLI null-field validation; no AIDrama impact. |
| `fcc23dcd` | `DEFER_NOT_RELEVANT` | Upstream merge commit containing deferred CLI work. |
| `6cd36b5a` | `DEFER_NOT_RELEVANT` | README/resource-only update. |
| `b610aaa5` | `DEFER_NOT_RELEVANT` | CLI error-message wording only. |
| `85d321df` | `IMPORT` | Security fix for HTTP download filename quoting. |
| `d7f79eac` | `DEFER_NOT_RELEVANT` | CLI test wording only. |
| `0b699b39` | `DEFER_NOT_RELEVANT` | Upstream merge commit containing the filename fix; imported selectively below. |
| `d288428d` | `IMPORT` | Regression coverage for safe download `Content-Disposition`. |
| `e6e736ad` | `IMPORT` | Portable special-character filename cases for the same regression. |
| `03c800bd` | `DEFER_NOT_RELEVANT` | Upstream merge commit containing deferred CLI work. |
| `465b8b35` | `DEFER_NOT_RELEVANT` | Redundant f-string cleanup in CLI. |

The upstream `248f658` upload-post settings fix is already before the
coordinated upstream baseline. The current AIDrama branch has no upload-post
settings surface; changing the original WebUI settings would be unrelated to
this reconciliation, so `UPLOAD_POST_SETTINGS_DELTA=DEFER_NOT_RELEVANT`.

## Imported security correction

`app/controllers/v1/video.py` previously built an unquoted
`Content-Disposition` header and reconstructed the name from `stem + suffix`.
The minimal correction passes the resolved file's complete name to Starlette's
`FileResponse(filename=...)`, which performs RFC 6266 quoting/UTF-8 fallback.
The existing path resolver remains the authority for filesystem containment;
no path validation was weakened.

`test/services/test_controller_video.py` now covers ASCII, spaces, Unicode,
and portable header-sensitive punctuation. On Windows, literal quote and
backslash characters cannot be created as filenames, so the punctuation case
(`=`, `;`, `(`, `)`) exercises the equivalent encoded-header path.

## Security audit

All 14 commits were reviewed for traversal, symlink, upload/download,
authentication, secret leakage, SSRF, deletion, and unsafe subprocess/path
handling changes. No additional unreconciled security fix affects AIDrama V1:
`NEW_UNRECONCILED_SECURITY_FIXES=0`.

No provider/live request, dependency installation, AIDrama architecture change,
or desktop UX change is part of this checkpoint.
