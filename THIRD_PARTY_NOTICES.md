# AIDrama Studio third-party notices

AIDrama Studio is derived from MoneyPrinterTurbo and retains the upstream MIT
license and attribution in `LICENSE` and `NOTICE`.

The Windows package is assembled from the exact dependency graph committed in
`uv.lock`. Every build must include:

- `release/sbom.cdx.json`, generated from the runtime lock closure;
- `release/package-files.sha256`, covering the physical packaged files;
- `release/build-provenance.json`, identifying product version, Git commit,
  schema version, platform and the hashes of both files above.

Python dependencies remain governed by their own license terms. The build
environment and final package inventory must be audited together; a source
dependency declaration alone is not proof that a component was distributed.
License texts supplied by a dependency's wheel or source distribution must be
retained when that dependency is present in the final package.

Important distribution boundaries:

- The current source environment's `imageio-ffmpeg` Windows binary reports an
  FFmpeg 7.1 build configured with `--enable-gpl --enable-version3` and GPL
  codecs. It must not be treated as an MIT component. Distribution requires a
  separately approved GPL compliance decision and the corresponding license /
  source-offer obligations.
- `resource/fonts/MicrosoftYaHei*.ttc` and `resource/fonts/STHeiti*.ttc` have
  no release-approved redistribution record in this repository. The AIDrama
  package definition does not add `resource/fonts`, and the release audit
  fails if those files appear in a package tree.
- PyInstaller, PyWebView and installer tooling are build-time tools only. They
  are not silently installed and must be audited at the versions actually used.

The detailed, evidence-scoped audit is maintained in
`docs/AIDRAMA_STUDIO_V1_0_LICENSE_AND_DISTRIBUTION.md`.

The Windows build also emits `THIRD_PARTY_NOTICES.txt` and a `licenses/`
directory from the exact dedicated build environment. `desktop/license_materials.py`
retains upstream wheel license/notice files and the exact bundled FFmpeg
`-version`/`-L` output; missing materials are marked for release review.
The checked-in `licenses/ffmpeg/` directory also carries the FFmpeg 7.1
upstream COPYING texts, GPL-enabled external-library COPYING texts, and an
exact-payload corresponding-source checklist.
