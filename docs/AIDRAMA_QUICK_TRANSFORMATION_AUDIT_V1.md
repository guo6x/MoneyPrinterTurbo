# AIDrama Studio / MoneyPrinterTurbo 快速转型审计

**Phase:** `AIDRAMA_QUICK_MPT_TRANSFORMATION_AUDIT_V1`<br>
**审计性质:** READ-ONLY（本 Phase 未修改源码、未安装依赖、未创建桌面启动器）<br>
**审计基线:** `d84f4f344f1434286603994eedc7f46330f456b3`

## CURRENT_ARCHITECTURE

### Entry points

|入口|真实落点|作用|
|---|---|---|
|ASGI API|`main.py:6-14` → `app.asgi:app`|调用 `uvicorn.run` 启动 FastAPI|
|FastAPI app|`app/asgi.py:get_application`, `app/asgi.py:app`|注册路由、异常处理、生命周期、静态文件|
|原 WebUI|`webui/Main.py`|Streamlit 单页应用；通过 `webui.bat`/`webui.sh` 启动|
|CLI|`cli.py:795` 及 `main()` 定义|命令行生成、配置和任务相关入口|
|容器|`Dockerfile*`, `docker-compose*.yml`|部署/发布环境，不是桌面入口|

`webui.bat` 已实现 Windows Python/uv/Streamlit 探测、8501-8599 端口探测，并执行 `streamlit run .\\webui\\Main.py`；`webui.sh` 提供对应 Unix 路径。

### WebUI structure

`webui/Main.py`（约 5367 行）集中承担页面、session state、配置表单、任务轮询、上传下载、预览和错误提示；样式在 `webui/styles.css`，本地化在 `webui/i18n/*.json`，Streamlit 配置在 `webui/.streamlit/webui.toml`。它直接导入 `app.services.{llm,material,voice,video,bgm,task,webui_task,...}`，因此适合作为 debug/fallback，不适合作为新的产品层继续膨胀。

### FastAPI structure

- `app/router.py:root_api_router` 聚合 `app/controllers/v1/video.py` 与 `app/controllers/v1/llm.py`。
- `app/controllers/v1/video.py:create_video/create_subtitle/create_audio/create_task` 创建任务；`get_all_tasks/get_task/delete_video` 查询、删除；`get_bgm_list/upload_bgm_file/get_video_materials_list/upload_video_material_file` 管理 BGM/本地素材。
- `app/controllers/v1/llm.py:generate_video_script/generate_video_terms/generate_video_social_metadata` 提供 LLM API。
- `app/asgi.py` 注册 `HttpException`、`RequestValidationError` 处理器，并挂载 `/tasks`（`utils.task_dir()`）与静态 `/`（`utils.public_dir()`）。CORS 由 `CORS_ALLOWED_ORIGINS` 控制，未设置时为 `*`。

### Task flow and task state

`app/controllers/manager/base_manager.py` 定义任务管理器协议和队列满异常；`memory_manager.py:InMemoryTaskManager` 为本地队列，`redis_manager.py:RedisTaskManager` 为 Redis 队列。`app/services/state.py` 提供 `MemoryState`、`RedisState` 和全局 `state`，状态常量在 `app/models/const.py`（失败 `-1`、完成 `1`、处理中 `4`）。

主编排在 `app/services/task.py`：

`start` → `_run_pipeline` → `generate_script` → `generate_terms` → `generate_audio` → `generate_subtitle` → `get_video_materials` → `generate_final_videos`。跨平台发布使用 `_schedule_cross_post`/线程池，`recover_interrupted_cross_posts` 在 `app/asgi.py:application_lifespan` 启动时恢复异常状态。`app/services/webui_task.py:submit_generation/_run_generation` 是 WebUI 侧的后台任务和日志捕获封装。

### Config flow

`app/config/config.py` 在模块级加载 `config.toml`（`load_config`/`save_config`），使用 `_SynchronizedConfig` 和 `runtime_config_lock`。WebUI 通过 `update_config_nonblocking/delete_config_nonblocking/try_save_config` 延迟写入，避免生成任务期间配置竞争。默认配置模板为 `config.example.toml`；运行数据根目录来自 `app/utils/utils.py:storage_dir`。

### LLM flow

`app/services/llm.py:_generate_response` 统一调用供应商；`build_script_prompt/generate_script` 生成脚本，`generate_terms` 生成素材搜索词，`generate_social_metadata` 生成平台文案。参数/响应 Schema 在 `app/models/schema.py:VideoScriptParams/VideoTermsParams/VideoSocialMetadataParams` 及对应 Request/Response 类。`app/models/llm_provider.py` 管理 provider registry、默认 provider 和 override 归一化。

### Stock material flow

`app/services/material.py:search_videos_pexels/search_videos_pixabay/search_videos_coverr` 搜索库存视频；`_search_videos_with_cache` 统一缓存；`download_videos` 和 `_download_videos_by_script_order` 下载并按脚本顺序组织素材；来源记录由 `_persist_material_sources` 写入任务目录。

### Local material flow

`app/controllers/v1/video.py:upload_video_material_file` 接收上传；`file_security.resolve_path_within_directory` 与 `_sanitize_upload_filename` 防止路径穿越；`app/services/material.py:save_video`、`video.preprocess_video` 负责保存/预处理。WebUI 在 `webui/Main.py` 中识别本地素材扩展名并将其转换为 `MaterialInfo`。

### TTS

`app/services/voice.py:tts` 是统一入口，按 voice provider 分派到 Edge、Azure、SiliconFlow、Gemini、Mimo、MiniMax、ElevenLabs、Chatterbox 等实现；`get_audio_duration` 获取时长，`create_subtitle` 将语音 cue 转为字幕。`app/services/task.py:generate_audio` 将 TTS 与自定义音频路径纳入同一任务阶段。

### Subtitle

`app/services/subtitle.py:create` 从音频生成字幕，`file_to_subtitles` 读取 SRT，`correct` 用脚本文本校正；`app/services/voice.py:create_subtitle` 处理 provider cue。任务阶段由 `app/services/task.py:generate_subtitle` 写入 `task_dir(task_id)/subtitle.srt`。

### BGM

`app/services/bgm.py:should_use_bgm`, `validate_bgm_upload`, `save_bgm_upload`, `list_bgm_files`, `resolve_bgm_file` 负责上传和本地 BGM；`app/services/task.py:_VIDEO_MUSIC_PROVIDERS`、`generate_final_videos` 接入 Sonilo/ElevenLabs 等生成型 BGM；`app/services/video.py:get_bgm_file` 和 `generate_video` 完成混音/渲染。

### Video render

`app/services/video.py:get_ffmpeg_binary`、`_effective_video_codec`、`concat_video_clips_with_ffmpeg` 管理 FFmpeg；`combine_videos` 组合素材、字幕、音频、BGM，`generate_video` 生成最终文件；`preprocess_video` 预处理本地/库存素材。`moviepy==2.2.1` 是主要剪辑库，FFmpeg 是外部运行时依赖。

### Artifacts and paths

`app/utils/utils.py:root_dir/storage_dir/resource_dir/task_dir/font_dir/song_dir/public_dir/get_ffmpeg_binary` 统一解析资源。每个任务目录为 `storage/tasks/<task_id>/`；`app/services/task_artifacts.py:_script_file/write_script_data/patch_script_data` 管理其中的 `script.json`；任务阶段通常还产生音频、`subtitle.srt`、下载素材、临时文件和 `final-*.mp4`。API 通过 `_task_file_to_uri` 将任务内文件映射为 `/tasks/...`。

### Tests

`test/` 下共有 41 个 Python 测试文件，覆盖 config、schema、LLM、material、voice、subtitle、video、task/state、controller、WebUI 服务、缓存和安全路径。审计环境使用项目 `.venv` 运行结果为 **564 passed, 10 skipped, 1 failed**；失败是 `test/services/test_webui_task.py::test_worker_logs_are_available_without_streamlit_session_state` 的 Windows 反斜杠路径与正斜杠断言差异，属于跨平台测试稳健性问题，需在实施阶段修复。

## REUSE_MATRIX

|AIDrama Studio 能力|EXISTING_REUSE|ADAPT|NEW|
|---|---|---|---|
|Project|`storage_dir`, `task_dir`, `config`|把产品项目 ID 与 MPT task_id 建立映射|AIDrama Project repository/service|
|Story Bible|`llm._generate_response`, `generate_script`, provider registry|增加结构化输出约束与持久化|StoryBible schema、解析/校验服务|
|Structured Script|`llm.generate_script`, `task.generate_script`, `task_artifacts.write_script_data`|从段落文本适配为 scene/beat/shot JSON|StructuredScript schema、版本服务|
|Character|LLM 能力、`MaterialInfo` 可作为素材引用|增加角色字段、引用关系|Character entity、角色库 UI|
|Location|素材搜索词 `generate_terms`、本地素材上传|增加地点描述与资产绑定|Location entity|
|Shot|`VideoParams`、`MaterialInfo`、`task.generate_terms`|增加 shot timing、镜头意图、风险和锁定字段|Shot entity、shot compiler|
|Shot List|`generate_terms`、`material.download_videos`|按 shot 而非仅按段落组织|Shot-list generator/validator|
|Risk Level|现有任务失败状态和日志|定义产品级风险枚举/门禁规则|Risk policy/QC rules|
|Asset|`MaterialInfo`、上传 API、`material_cache`、任务产物|统一库存/本地/生成素材元数据|Asset repository/index|
|Asset Lock|无明确锁定模型|可复用任务 busy 判断作为并发保护参考|Asset lock/version service|
|Production Task|`TaskManager`、`state`、`task.start/_run_pipeline`|AIDrama task 与 MPT task 分层关联|ProductionTask facade|
|QC|视频/字幕校验函数、任务状态、日志|把技术检查聚合为报告|QC service/rules/report|
|Review Gate|无产品级人工审核门|在任务编排外增加 gate 状态|Review entity、gate UI|
|Rough Cut|`video.combine_videos` 可生成视频产物|增加低清/草剪参数和标记|Rough-cut orchestration|
|Export|`generate_final_videos`、静态 `/tasks` 文件访问|增加导出配置、命名和打包 manifest|Export service|

结论：MPT 可复用“媒体执行”和“异步任务”能力，但 Project/Bible/Character/Shot/Review 等产品语义不是现有模型，不能假设已有实现。

## NEW_COMPONENTS

建议新增独立目录（本 Phase 不创建）：

```text
aidrama_studio/
  Main.py
  pages/       # 8 个产品页面
  components/  # 表格、时间线、资产卡、任务状态、review gate
  domain/      # Project/Bible/Character/Location/Shot/Asset/Review/Task DTO
  services/    # project、story、shot、asset、production、qc、export
  storage/     # sqlite3 repository + artifact path manager
```

该边界适合快速交付：`Main.py` 只负责 Streamlit 导航和依赖装配；产品服务调用 `app.services`，不得复制 `app/services/task.py` 的视频后端。原 `webui/Main.py` 保持独立运行。

## DATA_MODEL

采用 Python 标准库 `sqlite3` 作为 canonical project DB，项目 artifact 放在 `storage/aidrama/projects/<project_id>/`。Redis 仅保留 MPT runtime queue/state 用途，不作为项目真相源。

最小表（均含 `id`, `project_id`, `created_at`, `updated_at`，时间使用 UTC ISO-8601）：

```sql
project(id TEXT PRIMARY KEY, name TEXT, status TEXT, settings_json TEXT, created_at TEXT, updated_at TEXT)
story_bible(id TEXT PRIMARY KEY, project_id TEXT, version INTEGER, content_json TEXT, source TEXT, status TEXT, created_at TEXT, updated_at TEXT)
character(id TEXT PRIMARY KEY, project_id TEXT, name TEXT, profile_json TEXT, asset_id TEXT, created_at TEXT, updated_at TEXT)
location(id TEXT PRIMARY KEY, project_id TEXT, name TEXT, profile_json TEXT, asset_id TEXT, created_at TEXT, updated_at TEXT)
shot(id TEXT PRIMARY KEY, project_id TEXT, sequence_no INTEGER, scene_no TEXT, prompt TEXT, duration REAL, risk_level TEXT, asset_lock_id TEXT, status TEXT, data_json TEXT, created_at TEXT, updated_at TEXT)
asset(id TEXT PRIMARY KEY, project_id TEXT, kind TEXT, source TEXT, path TEXT, uri TEXT, metadata_json TEXT, checksum TEXT, status TEXT, created_at TEXT, updated_at TEXT)
review(id TEXT PRIMARY KEY, project_id TEXT, target_type TEXT, target_id TEXT, gate TEXT, status TEXT, comments TEXT, reviewer TEXT, created_at TEXT, updated_at TEXT)
production_task(id TEXT PRIMARY KEY, project_id TEXT, mpt_task_id TEXT, kind TEXT, state TEXT, progress INTEGER, error TEXT, artifacts_json TEXT, created_at TEXT, updated_at TEXT)
```

建议对 `project_id`, `shot.project_id`, `asset.project_id`, `production_task.mpt_task_id` 建索引；大文件不进 SQLite，只存相对路径、URI、checksum 和 metadata。

## PAGE_MAP

建议 `aidrama_studio/Main.py` 使用 Streamlit multipage/navigation，页面落点如下：

|页面|新代码落点|可复用|必须新写|
|---|---|---|---|
|1 工作台 Dashboard|`pages/01_Dashboard.py`|`webui_task.get_task_logs`, task/state 查询模式|项目概览、最近产物、门禁摘要|
|2 创意与剧本|`pages/02_Story.py`|`llm.generate_script`, `generate_terms`, WebUI 的 provider/settings 组件思路|Bible/结构化脚本编辑、版本和校验|
|3 角色与场景资产|`pages/03_Assets.py`|上传/下载安全逻辑、`MaterialInfo`, `material_cache`|Character/Location/Asset 卡片、锁定|
|4 分镜导演台|`pages/04_Shot_Director.py`|素材搜索和 `VideoParams`|Shot 表格、排序、风险、shot-to-asset 绑定|
|5 制作中心|`pages/05_Production.py`|`webui_task.submit_generation`, task manager/state|按 shot/项目派生 MPT task、队列和取消|
|6 QC / Review|`pages/06_Review.py`|状态、日志、字幕/字体检查函数|QC 报告、Review Gate、人工通过/退回|
|7 后期与成片|`pages/07_Post.py`|`video.combine_videos`, `generate_final_videos`, `/tasks` artifact 访问|rough-cut 选择、导出清单、命名和打包|
|8 Settings|`pages/08_Settings.py`|现有 config/LLM/TTS/BGM/locale 控件|项目级设置、凭据遮罩、桌面路径显示|

可复用的 Streamlit primitive 是 `st.*` 表单、上传、下载、进度、媒体预览和现有 i18n/style 资源；不可直接复用的是 `webui/Main.py` 的页面编排和 session state 结构，应抽取小型组件后在新页面中重新组织。

## HAPPY_PATH

1. **Create Project** — NEW：`aidrama_studio/services/project.py` 写入 SQLite `project`，创建 artifact 目录。
2. **Generate Story Bible** — NEW 编排 + EXISTING `app.services.llm._generate_response`/provider registry；结构化 schema、版本和持久化 NEW。
3. **Generate Structured Script** — ADAPT `app.services.llm.generate_script`（或 `build_script_prompt`）生成初稿；NEW parser/validator 将段落转为 scene/beat/shot JSON，写入 `story_bible`/artifact。
4. **Generate Shot List** — ADAPT `app.services.llm.generate_terms` 的关键词生成能力；NEW shot-list prompt、`Shot` 持久化和风险计算。
5. **Select/Generate Materials** — EXISTING `material.search_videos_pexels/search_videos_pixabay/search_videos_coverr`, `download_videos`, `save_video`, `video.preprocess_video`, 上传 controller；NEW Asset index、shot 绑定、Asset Lock。
6. **TTS** — EXISTING `app.services.task.generate_audio` → `app.services.voice.tts/get_audio_duration`；AIDrama 只需把 shot/script 文本编译成 `VideoParams`/任务输入。
7. **Subtitle** — EXISTING `app.services.task.generate_subtitle` → `voice.create_subtitle` 或 `subtitle.create/file_to_subtitles/correct`。
8. **Render** — EXISTING `app.services.task.generate_final_videos` → `video.combine_videos/generate_video`；NEW adapter 将 Shot List/Asset Lock 编译为 MPT `VideoParams` 与 `MaterialInfo`。
9. **QC** — PARTIAL EXISTING：`video.subtitle_colors_are_indistinguishable`, `subtitle_font_supports_text`, task state/logs；NEW QC aggregator、规则、报告和风险门禁。
10. **Review** — NEW：SQLite `review`、Review Gate 页面和批准/退回状态。
11. **Export MP4** — EXISTING 产物生成和 `/tasks` 静态访问；NEW export manifest、项目级命名/复制和导出状态。

在上述路径中，MPT 可直接执行的媒体步骤为 TTS、字幕、素材下载/预处理、BGM、FFmpeg/moviepy 渲染；Project/Bible/Structured Script/Shot/Asset Lock/QC/Review/Export orchestration 需要 AIDrama 产品层。

## DESKTOP_DECISION

### PyWebView + local Streamlit assessment

- **Streamlit launch:** 可复用 `webui.bat` 的命令形式，但桌面壳应直接 `subprocess.Popen([python, '-m', 'streamlit', 'run', ...])`，不要依赖当前工作目录。
- **Lifecycle:** 启动子进程后轮询健康 URL；窗口关闭时 terminate→短等待→kill，避免残留 Python/FFmpeg 子进程。
- **Port:** 不要固定 8501；使用 socket 绑定 `127.0.0.1:0` 或复用 `webui.bat` 的候选端口逻辑，并以实际 Streamlit URL 做 health check。
- **Upload/download:** WebView 的本地页面仍走 Streamlit upload/download；大文件下载、路径权限和 `/tasks` 映射需测试，不能直接暴露任意本地目录。
- **Browser behavior:** 桌面模式不应调用 `webbrowser.open`；当前 `webui/Main.py` 中的浏览器打开行为需通过桌面运行标记禁用/替换。
- **Shutdown cleanup:** Streamlit 子进程、后台线程、FFmpeg 和可能的 Redis 连接都要纳入清理；MPT 现有 lifespan 只记录 shutdown，不等价于桌面进程编排。
- **Config/resource paths:** `config.py` 当前以项目根目录 `config.toml` 为主；PyInstaller 后应把可写配置/SQLite/artifacts 放到 `%APPDATA%` 或 `%LOCALAPPDATA%`，只读资源放 bundle。
- **FFmpeg:** `utils.get_ffmpeg_binary`/`video.get_ffmpeg_binary` 需要验证打包后路径和 PATH；FFmpeg 二进制、字体、歌曲、模型缓存属于 onedir 资源规划重点。
- **PyInstaller onedir:** 可行性较高，但 Streamlit、moviepy、faster-whisper、Azure/云 SDK 和模型文件需要 hidden-import/data collection；onedir 比 onefile 更适合首版。
- **WebView2:** Windows WebView2 runtime 是系统前置条件，应检测并在安装器中说明/引导安装。

### 对比和推荐

|方案|优点|主要代价|结论|
|---|---|---|---|
|PyWebView|Python 体系改动最小，适合包住本地 Streamlit|WebView2、生命周期和打包细节需自行处理|**首版推荐**|
|Tauri|体积/性能较好，长期产品体验佳|Rust + Python sidecar 工程量更大|第二阶段候选|
|Electron|生态成熟、调试方便|体积大、内存占用高|若需要复杂前端可选|
|直接浏览器|零桌面封装成本|不满足安装包产品体验|保留为 debug/fallback|

推荐结论：先做 `PyWebView + local Streamlit` 的 Windows onedir 设计；本 Phase 不安装 pywebview、不创建 launcher、不做 packaging。

## RISKS

1. `webui/Main.py` 体量大，新功能若继续直接写入会形成回归和状态耦合。
2. AIDrama 的结构化领域对象与 MPT 的段落脚本/`VideoParams` 不同，需要 adapter，不应强行修改 MPT Schema。
3. MPT task/state 可能使用内存或 Redis；AIDrama SQLite 必须保存 canonical 状态，并记录 `mpt_task_id`，避免两套状态失配。
4. 外部 API、库存素材、TTS、模型和 FFmpeg 都可能失败；QC/review 必须允许失败、重试和人工退回。
5. Windows 路径、FFmpeg、字体、模型体积、WebView2 和杀进程清理是桌面发布风险。
6. 当前测试存在路径分隔符失败；新增产品层还需跨平台测试。
7. CORS 默认 `*`、本地静态任务目录和 API key 配置在桌面交付时需要收紧边界。

## IMPLEMENTATION_ORDER

1. 建立 `aidrama_studio/` 空壳和 SQLite repository，不触碰原 WebUI。
2. 定义 Project/StoryBible/Shot/Asset/ProductionTask 最小 DTO 与 MPT adapter。
3. 先实现 Dashboard、Story、Shot Director 三页，打通 SQLite + LLM 结构化结果。
4. 接入现有 material/voice/subtitle/video/task 服务，建立 `mpt_task_id` 映射。
5. 加入 Asset Lock、QC report、Review Gate，再做 rough cut/export manifest。
6. 补充 AIDrama service/repository 测试和 Windows 路径测试。
7. 最后重做整体 UI 视觉层，再进入 PyWebView/PyInstaller onedir 评估。

## EXACT_FILES_TO_TOUCH

### 本 Phase 实际修改

- `docs/AIDRAMA_QUICK_TRANSFORMATION_AUDIT_V1.md`（仅此文件）

### 后续实施预计新增/修改（本 Phase 未执行）

- 新增：`aidrama_studio/Main.py`、`aidrama_studio/pages/*.py`、`aidrama_studio/components/*.py`、`aidrama_studio/domain/*.py`、`aidrama_studio/services/*.py`、`aidrama_studio/storage/*.py`
- 适配时优先调用而非重写：`app/services/llm.py`、`app/services/material.py`、`app/services/voice.py`、`app/services/subtitle.py`、`app/services/bgm.py`、`app/services/video.py`、`app/services/task.py`、`app/services/webui_task.py`、`app/services/task_artifacts.py`、`app/utils/utils.py`
- 需要补测：`test/` 下对应 service/controller 测试，以及新增 `test/aidrama_studio/`

## FINAL_GATE

本次审计满足只读约束：未安装 dependency、未删除文件、未修改 MPT backend、未创建 desktop app。原 `webui/` 保留为独立运行的 debug/fallback。
