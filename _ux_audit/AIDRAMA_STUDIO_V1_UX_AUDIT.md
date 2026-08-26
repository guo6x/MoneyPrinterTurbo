# AIDrama Studio V1 UX audit (read-only)

Audit target: installed `AIDramaStudio.exe` build, backed by the local Streamlit surface at `127.0.0.1:8501`.

Base head: `18a2b1103fcb59264ee1aee5a6225da26cc59d7e`  
Audit date: 2026-08-26  
Scope: internal desktop product, PyWebView native window, Streamlit UI, no provider calls.

## Required report fields

`TOP_10_UX_CHANGES=see UX priority list below (items 1–10)`  
`PROPOSED_INFORMATION_ARCHITECTURE=创意 → 故事/剧本 → 角色与场景 → 分镜 → 制作 → 审片 → 成片; Settings demoted; AI Director contextual`  
`PROPOSED_WORKSPACE_SHELL=left workflow stages + center creative work + right contextual AI Director/inspector + header/footer project/stage/next action`

## Capture note

The installed native wrapper was launched and inspected. Its main window was present, but the desktop capture bridge failed on the pywebview border API (`SetIsBorderRequired … E_NOINTERFACE`) before a native screenshot could be read. The same local Streamlit surface used by the wrapper was therefore inspected in the in-app browser. This is the product UI, not a source-code mock.

The 1366×768 files are exact bitmaps. The browser viewport was verified as 1920×1080 (`window.innerWidth=1920`, `innerHeight=1080`); exact 1920×1080 clip files are included. The ordinary in-app screenshot path is capped to the host panel width, so the non-clip 1920 files are retained only as a secondary reference.

## Executive result

`FIRST_RUN_SCORE=2/5`

The product identity is clear, but the first five seconds do not answer “what do I do next?” The dashboard has a single sample project, while the main workflow CTA is below the fold inside a narrow card. Stage pages can render a blocking “请先选择一个项目” state with only a return button. After opening the sample project, the first meaningful path is still blocked by an unconfigured LLM and an unapproved Story Bible.

### Page scores (1 poor → 5 strong)

| Page | Clarity | Primary action | Visual hierarchy | Creative workflow | Technical leakage | Perceived quality | Composite |
|---|---:|---:|---:|---:|---:|---:|---:|
| Dashboard / 工作台 | 3 | 2 | 3 | 2 | 2 | 3 | **2.5** |
| Story / Script / 创意与剧本 | 3 | 3 | 2 | 4 | 2 | 3 | **2.8** |
| References / 角色与场景 | 2 | 1 | 2 | 2 | 3 | 2 | **2.0** |
| Shot Director / 分镜导演台 | 3 | 2 | 3 | 3 | 1 | 3 | **2.5** |
| Production / 制作中心 | 3 | 2 | 3 | 3 | 2 | 3 | **2.7** |
| Review / QC & Review | 2 | 1 | 2 | 2 | 3 | 2 | **2.0** |
| Post / 后期与成片 | 3 | 1 | 3 | 2 | 2 | 3 | **2.3** |
| Settings / 设置 | 3 | 3 | 2 | 1 | 1 | 3 | **2.2** |

`UX_P0_COUNT=3`  
`UX_P1_COUNT=7`

## First-time user test

### What the user can infer

- **What should I do first?** Open the single project card, or use “+ 新建短剧项目”. The card action is below the first viewport and does not say “继续到创意与剧本”.
- **Is it obvious in five seconds?** No. The sidebar lists eight destinations with no completion state, the dashboard stats (`1 / 0 / 0`) are not a next-step signal, and the primary action is not visually dominant.
- **What is the current project?** “验收测试项目” is visible on the card and on downstream pages once opened. It is not persistent in the shell header.
- **What is the current stage?** The stage is visible only inside Shot Director as `STORY`; other pages do not share a consistent stage/status header.
- **What should I do next?** The strongest answer is hidden behind the AI Director page’s `STOP_AND_REVIEW` recommendation. Story/References/Production each show gates, but the user must translate them into navigation.
- **Which button is primary?** The red action is inconsistent: “返回工作台”, “分析当前项目”, provider save, and creation actions all compete. Dashboard “打开” is not hero-level.

### Confusion, leakage, and breakage signals

- **Dangerous or confusing controls:** `删除`, `移除`, `清理安全临时文件`, `移除凭据`, “重新扫描诊断”, `Deploy`/`Stop` in the Streamlit chrome, and disabled generation/production buttons without a single adjacent fix action.
- **Terms requiring developer knowledge:** `Source Pack`, `Story Bible`, `Structured Script`, `Shot Plan`, `Production Job`, `RuntimePlan`, `Execution`, `Attempt`, `Provider`, `CapabilityRegistry`, `UNAVAILABLE`, `APPROVED`, `STOP_AND_REVIEW`, `FinalAssembly manifest`, SHA-256, DPAPI, SQLite, and internal IDs such as `char_001`/`loc_001`/`beat_001`.
- **Admin-panel feeling:** Settings is a provider registry and storage diagnostics console. Production’s “DIRECTOR PRODUCTION CONSOLE” and its advanced Execution section also feel operational rather than creative.
- **Unfinished feeling:** References can collapse to one warning, Review can be only “暂无 Production Job”, Post can be “尚未生成成片版本”, and Shot Plan can be only “请先完成并确认结构化剧本”. There is no inline “fix this now” route.
- **Feels broken while functional:** AI generation is disabled because every capability is `UNAVAILABLE`; the product does not provide a clear setup path in the creative flow. Blocking gates use raw English conditions. A stage entered by direct navigation may show “请先选择一个项目” even though the dashboard has a project.
- **Excessive scrolling:** Story/Script combines Brief, source pack, revision history, Story Bible sub-tabs, entity forms, and structured beats in one long page. Settings stacks model profiles, five capability selectors, provider accordions, storage, and diagnostics. Dashboard project actions are below the fold at 1366×768.
- **Weak information hierarchy:** large headings and orange eyebrow labels dominate, while readiness and next action are low-contrast. At 1920×1080 the creative column remains narrow with unused right-side space; at 1366×768 the primary card/action is below the fold.

## Information architecture audit

Target model: `创意 → 故事/剧本 → 角色与场景 → 分镜 → 制作 → 审片 → 成片`.

| Current surface | V1 disposition | Rationale |
|---|---|---|
| 工作台 / Dashboard | **KEEP** | Project home and resume point, but convert it to a stage-aware project overview. |
| 创意与剧本 | **KEEP** | Core creative stage. Split the current long form into a guided Brief → Story Bible → Script progression. |
| 角色与场景 | **KEEP as workflow stage; BECOME_CONTEXTUAL where possible** | It is a real creative deliverable, but most edits are triggered from story beats and shots. |
| 分镜导演台 / Shot Plan | **KEEP** | Shot list/editor is a distinct production deliverable. |
| AI 导演 / 制片 tab | **BECOME_CONTEXTUAL** | It is a recommendation/status layer, not a destination users should hunt for. |
| 制作中心 | **KEEP** | Execution and monitoring deserve a stage, with technical details collapsed. |
| QC & Review | **KEEP, rename Review** | Human review is a workflow gate; “QC” should be secondary copy (“技术检查”). |
| 后期与成片 | **KEEP, rename Final** | Final assembly and export are a clear terminal stage. |
| 设置 / System | **DEMOTE; MOVE_TO_ADVANCED** | Keep a short “AI connection health” entry in onboarding, but move registry, storage, and diagnostics out of the main creative rail. |

### AI Director decision

AI Director should become a contextual right-side assistant in V1. It should appear on Dashboard, Story, References, Shot Plan, Production, Review, and Final with stage-specific advice, blockers, and one-click links. Keep the current director service semantics and decision records; only change where the user sees them. A compact “Director history” drawer can preserve auditability without a separate top-level page.

## Normal user versus advanced user

### NORMAL_USER

Project name, synopsis, current stage, progress, brief, Story Bible summary, character/location names, shot list and status, review notes/decision, final preview/export, “AI connected / not connected”, and a human-readable readiness checklist.

### ADVANCED

Story Bible and Structured Script as concepts (with plain-language help), revision history, model profile scope (global vs current project), provider capability selection, risk level, technical-check summary, retry/queue controls, and a collapsed execution history.

### DIAGNOSTIC_ONLY

`RuntimePlan`, `Execution`, `Attempt`, `Production Job`/Job ID, provider task ID, artifact hashes and SHA-256, revision IDs, raw JSON, internal enum values (`STOP_AND_REVIEW`, `OPENING`, `TURNING_POINT`, `ENDING`, `UNAVAILABLE`), internal entity IDs (`char_001`, `loc_001`, `beat_001`), `CapabilityRegistry`, provider inventory, `ProductionOrchestrator`, `FinalAssembly manifest`, DPAPI, SQLite/Redis canonical-storage language, raw paths, and measured QC internals. Keep these behind a single Advanced/Diagnostics disclosure or exportable support bundle.

## Page-by-page redesign specification

### Dashboard / 工作台

- **CURRENT_PROBLEM:** Project card is narrow and below the fold; “打开/编辑/删除” are equal-weight controls; `1 / 0 / 0` does not communicate the current stage; import/export is presented as an expandable utility row.
- **NORMAL_USER_GOAL:** Choose or resume a project and immediately understand the next creative step.
- **PRIMARY_INFORMATION:** Project name, synopsis, last saved time, current stage, stage progress, and one recommended next action.
- **PRIMARY_ACTION:** `继续：创意与剧本` (or `打开项目` when no stage exists).
- **SECONDARY_ACTIONS:** New project, import/restore, export, switch project; edit/delete in a kebab menu with confirmation copy.
- **ADVANCED_DETAILS:** Project ID, package/revision metadata, hashes, import diagnostics.
- **REMOVE_OR_HIDE:** Aggregate counts without labels, `.aidrama` jargon, prominent delete, and Streamlit `Deploy` chrome from the user mental model.
- **RECOMMENDED_LAYOUT:** Project switcher/header → active-project hero with progress rail → sticky next-action CTA → recent projects list. Keep the active project visible in the shell on every page.

### Story / Script / 创意与剧本

- **CURRENT_PROBLEM:** Two nested tab systems plus a very long form; mixed Chinese/English; AI generation is disabled with only a provider error; approval gates are not paired with a fix path.
- **NORMAL_USER_GOAL:** Turn an idea into an approved story and structured script.
- **PRIMARY_INFORMATION:** Brief, Story Bible summary, script readiness, missing fields, and revision state.
- **PRIMARY_ACTION:** `创建空白 Story Bible` or `生成 Story Bible 草稿`; once valid, `确认 Story Bible` and then `生成结构化剧本`.
- **SECONDARY_ACTIONS:** Add source material, edit/reorder beats, save draft, view/compare revisions, open AI setup.
- **ADVANCED_DETAILS:** Revision IDs, entity IDs, order validation, raw structured payload, SHA-256.
- **REMOVE_OR_HIDE:** `Source Pack`, `TEXT_BRIEF`, raw hash text, raw DRAFT/APPROVED enums, and duplicate nested navigation from the normal view. Keep terms as tooltips/subtitles.
- **RECOMMENDED_LAYOUT:** Three-step progress header (Brief → Story Bible → Script), two-column editor (creative canvas + contextual assistant), sticky save/confirm bar, and a compact revision drawer.

### References / 角色与场景

- **CURRENT_PROBLEM:** A blocked page can be only “请先确认 Story Bible。” with no action; otherwise character/location editing is disconnected from the story beat that needs it.
- **NORMAL_USER_GOAL:** Define consistent characters and locations, add visual references, and lock approved references.
- **PRIMARY_INFORMATION:** Character/location cards, missing-reference count, lock status, and where each reference is used.
- **PRIMARY_ACTION:** `添加角色参考` / `添加场景参考`, then `锁定参考`.
- **SECONDARY_ACTIONS:** Import image, generate variants, replace/unlock, filter by story beat/shot.
- **ADVANCED_DETAILS:** Asset hash, provenance, cache path, internal role/location IDs, revision links.
- **REMOVE_OR_HIDE:** Raw IDs, hash strings, cache/storage paths, and a dead-end warning without a link back to Story Bible.
- **RECOMMENDED_LAYOUT:** Character/Location segmented control; card grid in the center; right inspector for selected asset; gate banner with a direct `回到 Story Bible` link when blocked.

### Shot Director / 分镜导演台

- **CURRENT_PROBLEM:** AI Director status and Shot Plan editing are separate tabs; first view exposes `STOP_AND_REVIEW`, raw gate strings, “生产就绪/高风险镜头/QC 失败” counters, and a generic `分析当前项目` button.
- **NORMAL_USER_GOAL:** Convert an approved script into a reviewable, executable shot list.
- **PRIMARY_INFORMATION:** Current beat, shot count, selected shot, unresolved creative questions, and reference coverage.
- **PRIMARY_ACTION:** `生成/编辑分镜`; if blocked, `解决前置事项` with links to the exact missing Story/Script/Reference item.
- **SECONDARY_ACTIONS:** Reorder, duplicate, lock, compare revision, open references, run AI analysis.
- **ADVANCED_DETAILS:** Risk enums, decision records, plan revision IDs, gate evaluations, provider/task metadata.
- **REMOVE_OR_HIDE:** `STOP_AND_REVIEW`, raw English gate text, internal counters, and a separate AI Director destination.
- **RECOMMENDED_LAYOUT:** Left shot list, center script-beat/preview canvas, right contextual AI Director/inspector. Retain a collapsible decision history drawer.

### Production / 制作中心

- **CURRENT_PROBLEM:** “整剧制作” reads like an admin console; the start button is disabled; raw English gates and `Production Job`/Execution details are more prominent than the readiness explanation.
- **NORMAL_USER_GOAL:** Start production once the project is ready and monitor each shot.
- **PRIMARY_INFORMATION:** Human-readable preflight checklist, queue progress, per-shot state, and the next fix.
- **PRIMARY_ACTION:** `开始制作` when ready; otherwise each checklist item is a link to fix it.
- **SECONDARY_ACTIONS:** Pause/resume, retry failed shot, cancel queued work, open preview, export report.
- **ADVANCED_DETAILS:** RuntimePlan, Execution, Attempt, Job ID, provider task ID, artifact hash, orchestrator logs.
- **REMOVE_OR_HIDE:** `DIRECTOR PRODUCTION CONSOLE`, `Create Production Job`, and raw execution prose from the normal surface.
- **RECOMMENDED_LAYOUT:** Readiness header + sticky CTA; shot progress table/filmstrip; right inspector for the selected shot; advanced execution drawer below.

### Review / QC & Review

- **CURRENT_PROBLEM:** When no job exists the page only says “暂无 Production Job”; there is no direct path to Production. “QC” is technical shorthand and the human review surface is not visible.
- **NORMAL_USER_GOAL:** Watch generated shots, resolve issues, and approve the cut for Final.
- **PRIMARY_INFORMATION:** Shot filmstrip, preview, technical-check badge, reviewer notes, and approval state.
- **PRIMARY_ACTION:** `播放并审核` → `通过并进入成片`.
- **SECONDARY_ACTIONS:** Run technical checks, accept/reject selected shot, retry, annotate, compare versions.
- **ADVANCED_DETAILS:** QC rule names, measured values, job/attempt IDs, provider IDs, logs and hashes.
- **REMOVE_OR_HIDE:** Raw “Production Job” language, technical QC internals, and empty state without a `去制作中心` CTA.
- **RECOMMENDED_LAYOUT:** Filmstrip left, viewer center, review checklist/comments right, batch approve/reject bar at the bottom.

### Post / 后期与成片

- **CURRENT_PROBLEM:** Readiness and history are passive messages; there is no visible assembly control or route to fix missing production/QC prerequisites.
- **NORMAL_USER_GOAL:** Assemble approved shots, preview the final video, and export a version.
- **PRIMARY_INFORMATION:** Approved-shot count, duration/aspect, captions/audio, selected final version, and export status.
- **PRIMARY_ACTION:** `生成成片预览` (or `去 Review` when prerequisites are missing).
- **SECONDARY_ACTIONS:** Choose version, captions/music, regenerate, compare history, export.
- **ADVANCED_DETAILS:** FinalAssembly manifest, assembly revision, artifact hashes, codec/container details.
- **REMOVE_OR_HIDE:** “manifest” terminology, raw paths, hashes, and a history panel that has no version.
- **RECOMMENDED_LAYOUT:** Preview/timeline center, export settings right, version history below, prerequisite banner with direct links.

### Settings / 设置

- **CURRENT_PROBLEM:** A long provider/storage/diagnostics panel exposes `CapabilityRegistry`, Provider inventory, `UNAVAILABLE`, DPAPI, SQLite paths, and cleanup controls to normal users. The page is an admin console with a creative product attached.
- **NORMAL_USER_GOAL:** Connect AI capabilities and understand whether the local media engine is ready.
- **PRIMARY_INFORMATION:** Human-readable connection health by capability, media engine status, and current scope (global/project).
- **PRIMARY_ACTION:** `连接 AI 服务` / `保存设置`.
- **SECONDARY_ACTIONS:** Choose profile scope, configure a provider, test one capability (with explicit cost warning), open data folder.
- **ADVANCED_DETAILS:** Provider inventory, registry resolution, raw config, DPAPI/SQLite paths, diagnostic scan and cleanup.
- **REMOVE_OR_HIDE:** Raw filesystem paths, Redis/SQLite canonical-storage language, internal capability enums, and destructive cleanup from the default view.
- **RECOMMENDED_LAYOUT:** Short setup wizard at top; capability cards with “已连接/未连接/需检查”; provider forms in drawers; one Advanced/Diagnostics section at the bottom.

## Workspace shell proposal (Streamlit-compatible)

```text
┌ project switcher · current stage · save state · primary next action ┐
├──────────────┬───────────────────────────────┬──────────────────────┤
│ workflow     │ current creative work         │ contextual AI         │
│ stages       │ brief / story / refs / shots  │ Director + inspector  │
│              │ production / review / final   │ blockers + next step  │
└──────────────┴───────────────────────────────┴──────────────────────┘
```

- **Left (220–240px):** Idea, Story/Script, Characters/Locations, Shots, Production, Review, Final. Each stage has a completion/status dot and a small blocked count. Settings sits under a separate “Workspace” group.
- **Center:** One creative surface at a time. Use `st.columns`/`st.container` and a max-width content column; avoid the current narrow card + unused 1920px canvas.
- **Right (300–340px):** Contextual AI Director/inspector with “Next recommended action”, blockers, selected-item metadata, and a short prompt box. Collapse to a drawer at 1366px.
- **Header:** Current project name, current stage chip, autosave state, and one primary next-action button. The user should never need to infer project/stage from a separate page.
- **Footer:** Sticky `Continue`/`Save`/`Back` actions. Gate messages become actionable links.
- **Advanced:** One consistent “Advanced / Diagnostics” disclosure. Raw IDs, JSON, hashes, provider task IDs, and filesystem details live there.
- **V1 constraint:** This is achievable with the existing Streamlit shell; no Electron, React, Tauri, or frontend rewrite is required.

## UX priority list

### UX-P0 (workflow prevention / product appears broken)

1. **No-project dead ends:** stage pages can show only “请先选择一个项目” and a return button. Add a persistent project selector plus inline `打开项目/新建项目` and direct recovery links.
2. **No guided provider recovery:** all AI capabilities show `UNAVAILABLE`; Story’s main generation action is disabled with no staged setup path. Add a human-readable connection gate and a one-click route to the minimum required capability.
3. **Gate dead ends:** Story Bible → Script → References → Shot → Production → Review → Final gates are displayed as raw conditions without a “fix this” action. Turn every blocker into a linked checklist and preserve the next action in the shell.

### UX-P1 (major confusion/friction)

4. Replace the dashboard’s card-first resume flow with a stage-aware hero and a single `继续` CTA.
5. Make AI Director contextual; keep Shot Plan editing as the only shot-stage destination.
6. Split Story/Script into a visible Brief → Story Bible → Script wizard with a sticky action bar; reduce nested tabs and long scrolling.
7. Redesign Review as a real filmstrip/viewer/decision surface; link “no production job” directly to Production.
8. Replace the Settings admin panel with a short connection-health setup and move registry/storage/diagnostics to Advanced.
9. Translate/annotate technical terms (`QC`, `Source Pack`, `Production Job`, `UNAVAILABLE`, approval states) and hide internal enums/IDs from normal users.
10. Fix responsive hierarchy: use the available 1920px canvas, keep the inspector collapsible at 1366px, and keep primary actions visible without scrolling.

### UX-P2/P3

Mixed-language labels, low-contrast eyebrow text, placeholder “PROJECT FRAME”, white disabled controls, and Streamlit `Deploy`/`Stop` chrome are polish issues after the P0/P1 work. They should not be treated as the primary V1 scope.

## Before screenshots

All screenshots are in [`_ux_audit/screenshots`](D:\github\MoneyPrinterTurbo\_ux_audit\screenshots). The `exact-*` 1920 files are the requested 1920×1080 clips; the `flow-*` 1366 files are exact 1366×768 in-project captures.

- [Dashboard 1366×768](D:\github\MoneyPrinterTurbo\_ux_audit\screenshots\flow-dashboard-1366x768.png) · [Dashboard 1920×1080](D:\github\MoneyPrinterTurbo\_ux_audit\screenshots\exact-dashboard-1920x1080.png)
- [Story/Script 1366×768](D:\github\MoneyPrinterTurbo\_ux_audit\screenshots\flow-story-script-1366x768.png) · [Story/Script 1920×1080](D:\github\MoneyPrinterTurbo\_ux_audit\screenshots\exact-story-script-1920x1080.png)
- [References 1366×768](D:\github\MoneyPrinterTurbo\_ux_audit\screenshots\flow-references-1366x768.png) · [References 1920×1080](D:\github\MoneyPrinterTurbo\_ux_audit\screenshots\exact-references-1920x1080.png)
- [Shot Director 1366×768](D:\github\MoneyPrinterTurbo\_ux_audit\screenshots\flow-shot-director-1366x768.png) · [Shot Director 1920×1080](D:\github\MoneyPrinterTurbo\_ux_audit\screenshots\exact-shot-director-1920x1080.png)
- [Production 1366×768](D:\github\MoneyPrinterTurbo\_ux_audit\screenshots\flow-production-1366x768.png) · [Production 1920×1080](D:\github\MoneyPrinterTurbo\_ux_audit\screenshots\exact-production-1920x1080.png)
- [Review 1366×768](D:\github\MoneyPrinterTurbo\_ux_audit\screenshots\flow-review-1366x768.png) · [Review 1920×1080](D:\github\MoneyPrinterTurbo\_ux_audit\screenshots\exact-review-1920x1080.png)
- [Post/Final 1366×768](D:\github\MoneyPrinterTurbo\_ux_audit\screenshots\flow-post-1366x768.png) · [Post/Final 1920×1080](D:\github\MoneyPrinterTurbo\_ux_audit\screenshots\exact-post-1920x1080.png)
- [Settings 1366×768](D:\github\MoneyPrinterTurbo\_ux_audit\screenshots\flow-settings-1366x768.png) · [Settings 1920×1080](D:\github\MoneyPrinterTurbo\_ux_audit\screenshots\exact-settings-1920x1080.png)

DOM evidence captured during the audit is in [`_ux_audit/dom`](D:\github\MoneyPrinterTurbo\_ux_audit\dom).

## Audit invariants

`SOURCE_FILES_CHANGED=0`  
`COMMITS_CREATED=0`  
`LIVE_PROVIDER_CALLS=0`

Only documentation, screenshots, and DOM evidence were created under `_ux_audit/`; no source, schema, provider, desktop build, release, or tracked product files were changed.
