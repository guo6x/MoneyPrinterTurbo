# AIDrama Studio Task001 — Product Shell & Project Foundation

## Architecture

Task001 adds an independent Streamlit product layer under `aidrama_studio/` while preserving the original `webui/Main.py`, CLI, FastAPI and `app/services/*` media engine.

- `aidrama_studio/Main.py`: product entry, CSS loading, storage initialization and shared navigation.
- `components/`: reusable navigation, headers, project cards, status badges and empty states.
- `pages/`: eight accessible product pages. Dashboard is functional; later workflow pages explicitly show COMING SOON.
- `domain/`: lightweight `Project`, `ProjectStatus` and `AspectRatio` types.
- `storage/`: SQLite connection, versioned migrations and project repository.
- `services/project.py`: validation, CRUD, artifact directory initialization and safe deletion behavior.

The new layer does not copy the MoneyPrinterTurbo backend. Future production work should call existing `app.services` through focused adapters.

## Run commands

```powershell
.venv\Scripts\python.exe -m streamlit run aidrama_studio\Main.py --server.address=127.0.0.1 --server.port=8502
```

The original WebUI remains available:

```powershell
.venv\Scripts\python.exe -m streamlit run webui\Main.py --server.address=127.0.0.1 --server.port=8501
```

## Database

- Canonical DB: `storage/aidrama/aidrama.db`
- Project artifacts: `storage/aidrama/projects/<project_id>/`
- Archived non-empty artifacts: `storage/aidrama/archived_projects/<project_id>-<UTC timestamp>/`
- Migration table: `schema_migrations`
- Current migration count: 1 (`001_projects`)

The database and directories initialize automatically on first startup. Redis is not used as the project database.

Deletion requires explicit confirmation. An empty project directory is removed; a non-empty directory is moved to the archive before its database record is deleted, so user material is never silently erased.

## Page map

1. 工作台 — project metrics, create/edit/open/delete, demo seed and recent projects.
2. 创意与剧本 — COMING SOON; project context is enforced.
3. 角色与场景 — COMING SOON.
4. 分镜导演台 — COMING SOON.
5. 制作中心 — COMING SOON; no MPT task submitted in Task001.
6. QC & Review — COMING SOON.
7. 后期与成片 — COMING SOON; no rendering in Task001.
8. 设置 — product/storage information, MIT attribution and MPT core import health.

The current project is stored only in `st.session_state.current_project_id`. Pages never infer a project independently and provide a route back to Dashboard when none is selected.

## Known limitations

- Story Bible, structured scripts, shot lists, assets, production tasks, QC, review and export are intentionally not implemented in Task001.
- The UI uses Streamlit session state for current selection; the canonical project data remains in SQLite across restarts.
- Project cover art is a deliberate placeholder.
- Task001 checks MPT core imports only; it does not execute media generation.
- Desktop packaging, PyWebView and PyInstaller are outside this task.
