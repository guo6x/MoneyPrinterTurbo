@@ -0,0 +1,363 @@
# AIDRAMA Forge MAX Wave 1 Cross-Branch Conflict / Dependency Audit

## Gate

| Field | Value |
|---|---|
| PHASE | AIDRAMA_FORGE_MAX_WAVE_1_CROSS_BRANCH_CONFLICT_DEPENDENCY_AUDIT |
| REFERENCE_BASE | 557b9af74a9ccb3e5f02445d9bb1fccd71a021f2 |
| COMMON_FEATURE_PARENT | 8317dfef54336b091bf67bb035bd6e918e84a561 |
| FEATURE_COUNT | 10 |
| AUDIT_ONLY | YES |
| PRODUCT_CODE_MODIFIED | NO |
| SOURCE_EVIDENCE_COMPLETE | YES |
| FINAL_GATE | PASS |

PASS means that this conflict/dependency audit is complete. It does not mean
that any feature is integrated, executable together, or approved for release.

## Evidence method and ancestry

Every supplied feature head is exactly one commit after
8317dfef54336b091bf67bb035bd6e918e84a561. The requested base 557b is a
descendant of 8317:

    8317 -- f901173 -- 557b
       \-- each of the ten feature heads

The required git diff --name-status 557b..HEAD matrix is therefore a live-fix
protection matrix: an M can mean that a head still contains the pre-live-fix
snapshot, not that the feature author edited the file. This report also checks
8317..HEAD to identify actual feature-writer overlap.

For each feature, a three-way virtual merge of 557b and the head was checked
with git merge-tree --write-tree --messages. Immediate text conflicts occur
for Reliability, Creative, AUTO, Continuity, and Audio. A clean merge-tree
result is never treated as a semantic approval.

## 557b live-fix protection

| Commit | Protected files | Behaviour that must remain |
|---|---|---|
| f901173c1e42ee2dae95e832a21fcfb078dcbabd | aidrama_studio/pages/director.py; test/aidrama_studio/test_director_page.py | A plan from an old approved Script cannot silently continue. The UI must offer a new draft from the current approved Script while preserving historical plans/jobs. |
| 557b9af74a9ccb3e5f02445d9bb1fccd71a021f2 | aidrama_studio/pages/review.py; aidrama_studio/storage/migrations.py; test/aidrama_studio/test_migrations.py; test/aidrama_studio/test_review_page.py | Review enumerates every persisted execution/QC pair in canonical shot order. Migration 033 preserves already-recorded continuity tables idempotently. |

DO_NOT_OVERWRITE_LIVE_FIX:

- aidrama_studio/pages/review.py
- aidrama_studio/pages/director.py
- aidrama_studio/storage/migrations.py
- test/aidrama_studio/test_review_page.py
- test/aidrama_studio/test_director_page.py
- test/aidrama_studio/test_migrations.py

All ten feature heads retain the 8317 version of these paths. Any manual
resolution, copied file, or selective patch must explicitly retain the listed
557b behaviours and prove them by test.

## File-overlap results

- TOTAL_CHANGED_FILES: 83 unique paths in the required 557b..HEAD matrix.
- PATH_FEATURE_ENTRIES: 157.
- OVERLAPPED_FILES: 12 paths changed by two or more heads.
- Paths changed by 3 or more heads: 9; by 5 or more heads: 9.
- HIGH_RISK_OVERLAPPED_FILES: 7 code paths: migrations.py, repositories.py,
  services/__init__.py, pages/review.py, pages/director.py,
  pages/postproduction.py, and pages/production.py.

The full required matrix is in Appendix A. M and A are the exact
git diff --name-status 557b..HEAD statuses; a blank cell means the head matches
557b for that path.

### Actual authored overlap, 8317..feature heads

| Writers | File | Resolution |
|---|---|---|
| Rel, Cre, Ref, Auto, Con, Aud | aidrama_studio/domain/__init__.py | Rebuild one complete ordered export list. |
| Cre, Dir | aidrama_studio/pages/director.py | Rebuild around the f901 stale-plan guard. |
| Gov, Aud | aidrama_studio/pages/postproduction.py | Combine human-review readiness with delivery state. |
| Gov, Rel | aidrama_studio/pages/production.py | Recheck source candidates and paid lifecycle controls together. |
| Gov, Vis | aidrama_studio/pages/review.py | Rebuild all-execution Review plus advisory Vision. |
| Rel, Set, Cre, Ref, Auto, Vis, Con, Aud, Dir | aidrama_studio/services/__init__.py | Rebuild central service barrel. |
| Auto, Aud | aidrama_studio/services/project_archive.py | Combine retention/cleanup contract. |
| Rel, Cre, Auto, Con, Aud | aidrama_studio/storage/migrations.py | One forward migration chain; every feature declares version 033. |
| Rel, Cre, Auto, Con, Aud | aidrama_studio/storage/repositories.py | One five-way method/import fusion. |
| Rel, Auto, Con, Aud | test/aidrama_studio/test_migrations.py | Rebuild assertions for the final migration chain. |

## Immediate merge conflicts and per-file resolution

| File | Finding | Required resolution |
|---|---|---|
| aidrama_studio/storage/migrations.py | Reliability, Creative, AUTO, Continuity, and Audio each text-conflict with 557b and each authored migration 033. | KEEP_BASE migration 033 exactly. MANUAL_MERGE new forward migrations: 034 paid ledger/artifact identity, 035 creative operations, 036 AUTO state/events/authorizations, 037 audiovisual delivery. Continuity reuses protected 033 schema and must not replace it. |
| test/aidrama_studio/test_migrations.py | Reliability, AUTO, Continuity, Audio conflict; each expects its own end version. | REBUILD_FROM_CONTRACT. Test fresh DB, 032 DB, live DB already recorded at 033, then every final migration through 037; reapply returns zero. |
| aidrama_studio/storage/repositories.py | Five actual writers; no single-head base conflict. | MANUAL_MERGE. Preserve Reliability provider-task/ledger/identity methods, Creative operation records, AUTO durable state/events/authorization, Continuity truth, and Audio dialogue/TTS/timeline methods in one repository/import surface. |
| aidrama_studio/services/__init__.py | Nine writers. | REBUILD_FROM_CONTRACT after all modules exist; selecting one side loses exports used by pages/tests. |
| aidrama_studio/domain/__init__.py | Six writers. | REBUILD_FROM_CONTRACT; import/serialization smoke every added model family. |
| aidrama_studio/pages/review.py | All heads differ from 557b; actual writers are Governance and Vision. Both virtual merges are text-clean. | REBUILD_FROM_CONTRACT: retain 557b _review_targets and selected execution/artifact mapping; add Governance source wording; load Vision only for that selected execution/artifact. |
| aidrama_studio/pages/director.py | All heads differ from 557b; actual writers are Creative and Director. | REBUILD_FROM_CONTRACT: retain f901 stale-plan guard, add Creative generation entry point, and pass an explicit repository to the new Director projection. |
| aidrama_studio/pages/postproduction.py | Governance and Audio overlap. | MANUAL_MERGE. A post plan derives from an approved Final Assembly; delivery state cannot imply picture-source approval. |
| aidrama_studio/services/final_assembly.py | Governance is the only author. | KEEP_FEATURE Governance semantics after Review fusion: exact artifact, latest human review, APPROVED required; QC/Vision never substitute. |
| production execution, worker, queue, runner, resolver, Wan adapters | Reliability owns the authored code. | SELECTIVE_PATCH with lifecycle bridge: no paid re-submit without provider identity; AUTO must surface reconciliation. |
| provider profiles, runtime foundation, model runtime, ai capabilities | Settings, Creative, and Vision own adjacent contracts. | MANUAL_MERGE one persisted manifest identity: Settings selects, Creative/LLM and Vision consume, Audio receives selected TTS or explicit offline injection. |

## Semantic conflicts

SEMANTIC_CONFLICTS = 10.

| ID | Conflict | Required fusion rule |
|---|---|---|
| A | Governance vs 557b Review/source-decision: Governance requires latest exact-artifact APPROVED human review; 557b selects every persisted execution/QC pair. | Build the Review target list first, then apply Governance eligibility to that exact candidate. |
| B | Reliability vs Wan lifecycle: Reliability adds paid create gate and UNCERTAIN_CREATE, while AUTO active provider states omit UNCERTAIN_CREATE. | Treat it as a reconciliation-only active state in AUTO, BackgroundRunner, worker, and UI; no automatic paid re-submit. |
| C | Settings vs Creative/Vision/TTS: Settings persists exact selection; Creative/LLM and Vision consume profile resolution; Audio constructs a private FakeTTSUniversalRuntime. | SettingsModelService/ProviderProfile is the sole selection authority. Fake TTS is explicit offline injection only. |
| D | Creative AI vs AUTO next_action: Creative has idempotent CreativePipelineOperation records; AUTO independently calls Story/Script/Shot services. | AUTO calls the Creative pipeline contract or shares its idempotency key; never create parallel action history. |
| E | Reference Agent vs AUTO readiness: Reference Agent observes the approved Story-Script-Shot chain; AUTO reimplements readiness/prompting over ReferenceAssetService. | AUTO consumes Reference Agent readiness/actions and delegates candidate/bind/lock to that authority. |
| F | Vision vs Review UI: persisted latest Vision analysis can attach to the wrong result after 557b multi-execution selection. | Query by selected execution and artifact. Vision remains advisory and cannot write review/source state. |
| G | Continuity vs Review/Director: Continuity has immutable provenance and human-confirmed paid repairs; Director has only a session optional adapter. | Bind a repository-scoped, read-only continuity adapter; no projected action may repair or change final truth. |
| H | Audio vs Final/Postproduction: Audio adds versioned dialogue/TTS/timeline/delivery; Governance makes Final source human-approved. | Require frozen approved Final Assembly source and preserve hashes; resolve TTS from Settings or explicit fake injection. |
| I | Director vs final projections: Director removes f901 guard, AUTO mode is only a link, and postdelivery is absent. | Rebuild Director last with source truth, durable AUTO state, bound Continuity, postdelivery boundary, and live guard. |
| J | migrations/repositories: five heads each extend both from 8317 and claim migration 033. | One hand-authored migration/repository integration unit; never cherry-pick branch-local versions wholesale. |

## Dependency graph

### HARD_DEPENDENCIES

    GOVERNANCE -> AUTO
    GOVERNANCE -> AUDIO
    GOVERNANCE -> DIRECTOR
    RELIABILITY -> AUTO
    SETTINGS -> CREATIVE
    SETTINGS -> VISION
    SETTINGS -> AUDIO
    CREATIVE -> REFERENCE_AGENT
    REFERENCE_AGENT -> AUTO

### SOFT_DEPENDENCIES

    CREATIVE -> DIRECTOR
    AUTO -> VISION
    AUTO -> DIRECTOR
    VISION -> CONTINUITY
    VISION -> DIRECTOR
    CONTINUITY -> DIRECTOR
    AUDIO -> DIRECTOR

### NO_DEPENDENCY

- Reliability -> Reference Agent.
- Reference Agent -> Vision.
- Vision -> Audio other than their shared Settings dependency.
- Continuity -> Audio.

## Cherry-pick feasibility

| Feature | Classification | Basis |
|---|---|---|
| GOVERNANCE | CHERRY_PICK_WITH_CONFLICT_RESOLUTION | Virtual merge is clean but Review/Final source semantics need explicit inspection and tests. |
| RELIABILITY | SELECTIVE_PATCH_ONLY | Migration 033 conflicts and lifecycle state requires AUTO bridge. |
| SETTINGS | SELECTIVE_PATCH_ONLY | It must become shared selection authority, verified through Creative, Vision, and TTS. |
| CREATIVE | SELECTIVE_PATCH_ONLY | Migration/repository/Director overlap and duplicate AUTO intent risk. |
| REFERENCE_AGENT | CHERRY_PICK_WITH_CONFLICT_RESOLUTION | No base text conflict, but it must become AUTO's readiness authority. |
| AUTO | MANUAL_REIMPLEMENTATION_REQUIRED | Migration/repository conflict, duplicated Reference logic, and missing UNCERTAIN_CREATE. |
| VISION | SELECTIVE_PATCH_ONLY | Text-clean Review merge but requires selected-target advisory semantics and Settings. |
| CONTINUITY | SELECTIVE_PATCH_ONLY | Keep protected migration 033, then bind repository/domain/service manually. |
| AUDIO | SELECTIVE_PATCH_ONLY | Migration/repository/Postproduction fusion plus private TTS runtime conflict. |
| DIRECTOR | MANUAL_REIMPLEMENTATION_REQUIRED | Strict isolation fails by construction; live guard and projections require redesign. |

CHERRY_PICK_SAFE = NONE.

SELECTIVE_PATCH_REQUIRED = Reliability, Settings, Creative, Vision, Continuity,
Audio.

MANUAL_MERGE_REQUIRED = Governance, Reference Agent, AUTO, Director, migrations,
repositories, service/domain barrels.

## Director special case

### DIRECTOR_ISOLATION_ROOT_CAUSE

DirectorWorkspaceProjectionService accepts a repository but defaults to
ProjectRepository(). pages/director.py constructs it with no repository.
ProjectRepository() calls initialize_database with no paths, which resolves
AIDRAMA_DATA_DIR or default user application data. The Director page can
therefore bind to a different/default database from the active product context,
and construction can initialize/migrate that default store. Feature tests pass
an explicit temporary repository, so they do not cover page construction.

### Required remediation

1. Build one repository/context at application entry and inject it into page
   services and DirectorWorkspaceProjectionService.
2. Require an injected repository in UI construction; do not use its default
   repository fallback.
3. Bind a read-only Continuity adapter to the same repository.
4. Read AutoOrchestrator durable state for AUTO mode; a navigation link is not
   an AUTO projection.
5. Add separate postdelivery projection if Director must display Audio delivery.
   Final source truth must remain separate.
6. Retain f901 stale-plan protection.

| Upstream | Current Director feature | Remediation |
|---|---|---|
| Vision | Reads persisted advisory analysis. | Keep read-only and artifact-scoped. |
| Continuity | Optional session callable; normally NOT_AVAILABLE. | Repository-bound read-only adapter. |
| AUTO | Mode surface only links to Production. | Read durable AutoOrchestrator state/reasons. |
| Final | Uses FinalAssembly selected source plus QC/review. | Governance source truth first. |
| Postproduction / Audio | No delivery-final projection. | Add explicit delivery status or document scope boundary. |

## Required regression gates at every fusion point

| Fusion point | Required regression |
|---|---|
| Baseline protection | test_migrations.py; test_review_page.py; test_director_page.py; canonical 60-second fixture validation and canonical service-path tests. |
| Governance | test_candidate_and_source_truth.py; test_final_assembly.py; test_postproduction_page.py; test_review_page.py; test_director_page.py. |
| Reliability | test_production_reliability_cost_guard.py; test_production_execution.py; test_production_queue.py; test_production_worker.py; test_production_orchestrator.py; test_migrations.py. |
| Settings | test_universal_model_settings.py; test_provider_selection.py; model-runtime resolver/manifest tests; Creative LLM and Vision saved-project override smoke, with no provider call. |
| Creative | test_creative_pipeline.py; test_creative_intake.py; test_llm_invocation_gateway.py; Story/Script/Shot tests; test_director_page.py. |
| Reference Agent | test_reference_agent.py; test_assets_page.py; test_reference_asset_core.py; production-readiness tests. |
| AUTO | test_auto_orchestrator.py; test_shared_ux_contract.py; production queue/orchestrator tests; Governance final-source tests. |
| Vision | test_vision_universal_runtime.py; test_review_page.py; test_production_qc_review_page.py; test_provider_selection.py. |
| Continuity | test_continuity_engine.py; test_migrations.py; test_director_workspace.py. |
| Audio | test_audiovisual_pipeline.py; test_postproduction_page.py; Final Assembly/Postproduction tests; test_migrations.py. |
| Director last | test_director_workspace.py; test_director_page.py; test_review_page.py; test_final_assembly.py; strict page-construction isolation test. |

Mandatory new integration tests:

- Upgrade a database already recorded at live version 033 through version 037.
- AUTO sees UNCERTAIN_CREATE as reconciliation-only and submits no create on
  cold restart.
- Page-level Director construction uses supplied temporary paths while default
  AIDRAMA_DATA_DIR is poisoned.
- Audio resolves saved TTS selection or fails closed; fake runtime is explicit.
- Multi-shot Review combines 557b target selection, Governance source gate,
  and per-artifact Vision advisory.

## Recommended integration order

1. Freeze 557b behaviours; hand-plan migration/repository fusion and keep
   live migration 033.
2. GOVERNANCE.
3. RELIABILITY, adding forward migration 034.
4. SETTINGS.
5. CREATIVE, adding forward migration 035.
6. REFERENCE_AGENT.
7. AUTO, adding forward migration 036.
8. VISION.
9. CONTINUITY, retaining protected migration 033.
10. AUDIO, adding forward migration 037.
11. DIRECTOR last.

## Final report fields

    PHASE=AIDRAMA_FORGE_MAX_WAVE_1_CROSS_BRANCH_CONFLICT_DEPENDENCY_AUDIT
    REFERENCE_BASE=557b9af74a9ccb3e5f02445d9bb1fccd71a021f2
    FEATURE_COUNT=10
    TOTAL_CHANGED_FILES=83 unique paths; 157 path-feature entries
    OVERLAPPED_FILES=12
    HIGH_RISK_OVERLAPPED_FILES=7
    LIVE_557_FIX_FILES=pages/director.py; pages/review.py; storage/migrations.py; test_director_page.py; test_migrations.py; test_review_page.py
    DO_NOT_OVERWRITE_LIVE_FIX=the six LIVE_557_FIX_FILES above
    HARD_DEPENDENCIES=Governance->AUTO/AUDIO/DIRECTOR; Reliability->AUTO; Settings->Creative/Vision/Audio; Creative->Reference Agent; Reference Agent->AUTO
    SOFT_DEPENDENCIES=Creative->Director; AUTO->Vision/Director; Vision->Continuity/Director; Continuity->Director; Audio->Director
    SEMANTIC_CONFLICTS=10, detailed above
    MIGRATION_CONFLICTS=5 immediate text conflicts; one systemic version-033 collision
    REPOSITORY_CONFLICTS=5 writers; mandatory five-way manual fusion
    RUNTIME_CONFLICTS=3: Reliability/AUTO UNCERTAIN_CREATE; Settings consumer identity; Audio private Fake TTS bypass
    REVIEW_FINAL_CONFLICTS=3: live multi-shot target selection; Governance human approval; Vision advisory scoping
    UI_PROJECTION_CONFLICTS=5: Review/Vision target scope; Continuity adapter; AUTO state; postdelivery absence; Director stale-plan deletion
    CHERRY_PICK_SAFE=NONE
    SELECTIVE_PATCH_REQUIRED=Reliability, Settings, Creative, Vision, Continuity, Audio
    MANUAL_MERGE_REQUIRED=Governance, Reference Agent, AUTO, Director, migrations, repositories, service/domain barrels
    DIRECTOR_ISOLATION_ROOT_CAUSE=un-injected ProjectRepository default construction resolves default AIDRAMA_DATA_DIR
    RECOMMENDED_INTEGRATION_ORDER=Governance, Reliability, Settings, Creative, Reference Agent, AUTO, Vision, Continuity, Audio, Director
    PER_FEATURE_TEST_GATES=Required regression gates table
    PRODUCT_CODE_MODIFIED=NO
    SOURCE_EVIDENCE_COMPLETE=YES
    FINAL_GATE=PASS

## Appendix A: complete required 557b..HEAD matrix

| File | Gov | Rel | Set | Cre | Ref | Auto | Vis | Con | Aud | Dir |
|---|---|---|---|---|---|---|---|---|---|---|
| aidrama_studio/components/navigation.py |  |  |  |  |  | M |  |  |  |  |
| aidrama_studio/domain/__init__.py |  | M |  | M | M | M |  | M | M |  |
| aidrama_studio/domain/auto_orchestrator.py |  |  |  |  |  | A |  |  |  |  |
| aidrama_studio/domain/continuity.py |  |  |  |  |  |  |  | A |  |  |
| aidrama_studio/domain/creative_pipeline.py |  |  |  | A |  |  |  |  |  |  |
| aidrama_studio/domain/final_assembly.py | M |  |  |  |  |  |  |  |  |  |
| aidrama_studio/domain/post_production.py |  |  |  |  |  |  |  |  | M |  |
| aidrama_studio/domain/production_reliability.py |  | A |  |  |  |  |  |  |  |  |
| aidrama_studio/domain/reference_agent.py |  |  |  |  | A |  |  |  |  |  |
| aidrama_studio/Main.py |  |  |  | M |  |  |  |  |  |  |
| aidrama_studio/pages/assets.py |  |  |  |  | M |  |  |  |  |  |
| aidrama_studio/pages/auto.py |  |  |  |  |  | A |  |  |  |  |
| aidrama_studio/pages/creative.py |  |  |  | M |  |  |  |  |  |  |
| aidrama_studio/pages/director_workspace.py |  |  |  |  |  |  |  |  |  | A |
| aidrama_studio/pages/director.py | M | M | M | M | M | M | M | M | M | M |
| aidrama_studio/pages/postproduction.py | M |  |  |  |  |  |  |  | M |  |
| aidrama_studio/pages/production.py | M | M |  |  |  |  |  |  |  |  |
| aidrama_studio/pages/review.py | M | M | M | M | M | M | M | M | M | M |
| aidrama_studio/pages/settings.py |  |  | M |  |  |  |  |  |  |  |
| aidrama_studio/pages/story.py |  |  |  | M |  |  |  |  |  |  |
| aidrama_studio/services/__init__.py |  | M | M | M | M | M | M | M | M | M |
| aidrama_studio/services/adapters/mainland_wan.py |  | M |  |  |  |  |  |  |  |  |
| aidrama_studio/services/adapters/seedance_video.py |  | M |  |  |  |  |  |  |  |  |
| aidrama_studio/services/adapters/wan_video.py |  | M |  |  |  |  |  |  |  |  |
| aidrama_studio/services/ai_capabilities.py |  |  |  |  |  |  | M |  |  |  |
| aidrama_studio/services/audiovisual.py |  |  |  |  |  |  |  |  | A |  |
| aidrama_studio/services/auto_orchestrator.py |  |  |  |  |  | A |  |  |  |  |
| aidrama_studio/services/background_runner.py |  | M |  |  |  |  |  |  |  |  |
| aidrama_studio/services/continuity.py |  |  |  |  |  |  |  | A |  |  |
| aidrama_studio/services/creative_intake.py |  |  |  | M |  |  |  |  |  |  |
| aidrama_studio/services/creative_pipeline.py |  |  |  | A |  |  |  |  |  |  |
| aidrama_studio/services/director_workspace.py |  |  |  |  |  |  |  |  |  | A |
| aidrama_studio/services/final_assembly.py | M |  |  |  |  |  |  |  |  |  |
| aidrama_studio/services/heavy_job_runner.py |  | M |  |  |  |  |  |  |  |  |
| aidrama_studio/services/heavy_jobs.py |  | M |  |  |  |  |  |  |  |  |
| aidrama_studio/services/llm_runtime.py |  |  |  | M |  |  |  |  |  |  |
| aidrama_studio/services/mainland_frontend_runtime.py |  |  | M |  |  |  |  |  |  |  |
| aidrama_studio/services/model_runtime/__init__.py |  |  |  | M |  |  |  |  |  |  |
| aidrama_studio/services/model_runtime/llm.py |  |  |  | A |  |  |  |  |  |  |
| aidrama_studio/services/model_runtime/mainland_manifests.py |  |  | M |  |  |  |  |  |  |  |
| aidrama_studio/services/model_settings.py |  |  | A |  |  |  |  |  |  |  |
| aidrama_studio/services/postproduction.py |  |  |  |  |  |  |  |  | M |  |
| aidrama_studio/services/production_artifact_storage.py |  | M |  |  |  |  |  |  |  |  |
| aidrama_studio/services/production_execution.py |  | M |  |  |  |  |  |  |  |  |
| aidrama_studio/services/production_queue.py |  | M |  |  |  |  |  |  |  |  |
| aidrama_studio/services/production_recovery.py |  | A |  |  |  |  |  |  |  |  |
| aidrama_studio/services/production_reliability.py |  | A |  |  |  |  |  |  |  |  |
| aidrama_studio/services/production_runtime_resolver.py |  | M |  |  |  |  |  |  |  |  |
| aidrama_studio/services/production_worker.py |  | M |  |  |  |  |  |  |  |  |
| aidrama_studio/services/project_archive.py |  |  |  |  |  | M |  |  | M |  |
| aidrama_studio/services/provider_profiles.py |  |  | M |  |  |  |  |  |  |  |
| aidrama_studio/services/providers/__init__.py |  |  |  |  |  |  | M |  |  |  |
| aidrama_studio/services/providers/universal_vision.py |  |  |  |  |  |  | A |  |  |  |
| aidrama_studio/services/reference_agent.py |  |  |  |  | A |  |  |  |  |  |
| aidrama_studio/services/runtime_foundation.py |  |  | M |  |  |  |  |  |  |  |
| aidrama_studio/services/script.py |  |  |  | M |  |  |  |  |  |  |
| aidrama_studio/services/security.py |  | M |  |  |  |  |  |  |  |  |
| aidrama_studio/services/shot.py |  |  |  | M |  |  |  |  |  |  |
| aidrama_studio/services/story.py |  |  |  | M |  |  |  |  |  |  |
| aidrama_studio/services/vision_qc.py |  |  |  |  |  |  | M |  |  |  |
| aidrama_studio/storage/migrations.py | M | M | M | M | M | M | M | M | M | M |
| aidrama_studio/storage/repositories.py |  | M |  | M |  | M |  | M | M |  |
| aidrama_studio/styles.css |  |  |  |  |  |  |  |  |  | M |
| test/aidrama_studio/test_assets_page.py |  |  |  |  | M |  |  |  |  |  |
| test/aidrama_studio/test_audiovisual_pipeline.py |  |  |  |  |  |  |  |  | A |  |
| test/aidrama_studio/test_auto_orchestrator.py |  |  |  |  |  | A |  |  |  |  |
| test/aidrama_studio/test_candidate_and_source_truth.py | M |  |  |  |  |  |  |  |  |  |
| test/aidrama_studio/test_continuity_engine.py |  |  |  |  |  |  |  | A |  |  |
| test/aidrama_studio/test_creative_pipeline.py |  |  |  | A |  |  |  |  |  |  |
| test/aidrama_studio/test_desktop_branding.py |  |  |  |  |  |  | M |  |  |  |
| test/aidrama_studio/test_director_page.py | M | M | M | M | M | M | M | M | M | M |
| test/aidrama_studio/test_director_workspace.py |  |  |  |  |  |  |  |  |  | A |
| test/aidrama_studio/test_final_assembly.py | M |  |  |  |  |  |  |  |  |  |
| test/aidrama_studio/test_migrations.py | M | M | M | M | M | M | M | M | M | M |
| test/aidrama_studio/test_postproduction_page.py | M |  |  |  |  |  |  |  |  |  |
| test/aidrama_studio/test_production_queue.py |  | M |  |  |  |  |  |  |  |  |
| test/aidrama_studio/test_production_reliability_cost_guard.py |  | A |  |  |  |  |  |  |  |  |
| test/aidrama_studio/test_production_worker.py |  | M |  |  |  |  |  |  |  |  |
| test/aidrama_studio/test_reference_agent.py |  |  |  |  | A |  |  |  |  |  |
| test/aidrama_studio/test_review_page.py | M | M | M | M | M | M | M | M | M | M |
| test/aidrama_studio/test_shared_ux_contract.py |  |  |  |  |  | M |  |  |  |  |
| test/aidrama_studio/test_universal_model_settings.py |  |  | A |  |  |  |  |  |  |  |
| test/aidrama_studio/test_vision_universal_runtime.py |  |  |  |  |  |  | A |  |  |  |
