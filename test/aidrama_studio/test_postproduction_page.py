from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

from aidrama_studio.pages import postproduction as page


def _job(status="SUCCEEDED"):
    return SimpleNamespace(id="job-1", status=status, shot_plan_revision_id="plan-1")


def _attempt(status="SUCCEEDED", number=1, output="final/assembly-1/episode.mp4"):
    return SimpleNamespace(
        id=f"attempt-{number}",
        attempt_number=number,
        status=status,
        adapter_name="mpt-media-concat",
        output_relative_path=output,
        metadata_json={
            "duration_seconds": 3.2,
            "resolution": "320x240",
            "codec": "h264",
            "audio_stream": False,
            "size_bytes": 1024,
            "sha256": "a" * 64,
        },
        created_at="2026-08-24T10:00:00+00:00",
        finished_at="2026-08-24T10:01:00+00:00",
        error_message="D:\\private\\traceback",
    )


def test_final_page_contains_product_facing_sections():
    source = Path(page.__file__).read_text(encoding="utf-8")
    for label in (
        "后期与成片",
        "成片准备度",
        "生成成片",
        "成片历史",
        "导出 MP4",
        "高级信息 / 调试信息",
    ):
        assert label in source
    for label in ("最终后期", "字幕", "TTS", "BGM", "导出 SRT", "渲染最终后期成片"):
        assert label in source
    assert "水印" not in source


def test_blocked_readiness_shows_clear_reasons_without_generate_action():
    app = AppTest.from_string(
        """
from types import SimpleNamespace
from aidrama_studio.pages import postproduction as page
project = SimpleNamespace(id='project-1', title='Blocked project')
job = SimpleNamespace(id='job-1', status='FAILED', shot_plan_revision_id='plan-1')
class Production:
    def list_jobs(self, project_id): return [job]
    def validate_job_readiness(self, project_id, revision_id=None):
        return {'ready': False, 'total_shots': 3, 'eligible_shots': 2, 'blocked_shots': 1,
                'estimated_duration': 4, 'blocked_reasons': ['shot_003: QC result 不是 QC_PASS']}
class Manifest:
    repository = None
    def list_assemblies(self, project_id, job_id): return []
class Runtime:
    def __init__(self, repository=None): pass
    def list_attempts(self, project_id, assembly_id): return []
page.current_project_or_stop = lambda: project
page.ProductionService = Production
page.FinalAssemblyService = Manifest
page.FinalAssemblyRuntimeService = Runtime
page.render()
"""
    ).run(timeout=30)
    assert not app.exception
    assert any("成片尚未就绪" in item.value for item in app.warning)
    assert any("镜头尚未通过 QC" in item.value for item in app.markdown)
    assert not any(button.label == "生成最终成片" for button in app.button)


def test_ready_state_exposes_primary_generate_action():
    app = AppTest.from_string(
        """
from types import SimpleNamespace
from aidrama_studio.pages import postproduction as page
project = SimpleNamespace(id='project-1', title='Ready project')
job = SimpleNamespace(id='job-1', status='SUCCEEDED', shot_plan_revision_id='plan-1')
class Production:
    def list_jobs(self, project_id): return [job]
    def validate_job_readiness(self, project_id, revision_id=None):
        return {'ready': True, 'total_shots': 3, 'eligible_shots': 3, 'blocked_shots': 0,
                'estimated_duration': 4, 'blocked_reasons': []}
class Manifest:
    repository = None
    def list_assemblies(self, project_id, job_id): return []
class Runtime:
    def __init__(self, repository=None): pass
    def list_attempts(self, project_id, assembly_id): return []
page.current_project_or_stop = lambda: project
page.ProductionService = Production
page.FinalAssemblyService = Manifest
page.FinalAssemblyRuntimeService = Runtime
page.render()
"""
    ).run(timeout=30)
    assert not app.exception
    assert any(button.label == "生成最终成片" and not button.disabled for button in app.button)
    assert any("我已确认当前镜头顺序" in item.label for item in app.checkbox)
    assert any("3" in item.value for item in app.metric)


def test_unchecked_final_confirmation_never_creates_or_enqueues(monkeypatch):
    calls = []

    class Manifest:
        repository = None

        def create_assembly(self, *args, **kwargs):
            calls.append(("create", args, kwargs))

    monkeypatch.setattr(page.st, "checkbox", lambda *args, **kwargs: False)
    monkeypatch.setattr(page.st, "button", lambda *args, **kwargs: True)
    page._render_action(
        SimpleNamespace(id="project-1"),
        SimpleNamespace(id="job-1"),
        {"ready": True},
        None,
        Manifest(),
        SimpleNamespace(),
    )

    assert calls == []


def test_safe_download_name_does_not_leak_path():
    assert page._safe_download_name("D:\\private\\Project / final") == "DprivateProject  final-final.mp4"
    assert page._safe_relative_path("D:\\private\\episode.mp4") == "[项目相对路径不可用]"
    assert page._safe_relative_path("final/assembly/episode.mp4") == "final/assembly/episode.mp4"


def test_post_plan_selection_never_crosses_visible_assembly_chain():
    plans = [
        SimpleNamespace(id="old", source_final_assembly_id="assembly-old"),
        SimpleNamespace(id="current-1", source_final_assembly_id="assembly-current"),
        SimpleNamespace(id="other-newest", source_final_assembly_id="assembly-other"),
        SimpleNamespace(id="current-2", source_final_assembly_id="assembly-current"),
    ]

    assert page._plan_for_assembly(plans, "assembly-current").id == "current-2"
    assert page._plan_for_assembly(plans, "missing") is None


def test_successful_assembly_renders_preview_metadata_and_export(tmp_path):
    output = tmp_path / "episode.mp4"
    output.write_bytes(b"fixture-mp4")
    output_literal = str(output).replace("\\", "\\\\")
    app = AppTest.from_string(
        f"""
from types import SimpleNamespace
from pathlib import Path
from aidrama_studio.pages import postproduction as page
project = SimpleNamespace(id='project-1', title='Preview project')
job = SimpleNamespace(id='job-1', status='SUCCEEDED', shot_plan_revision_id='plan-1')
assembly = SimpleNamespace(id='assembly-1', status='SUCCEEDED', production_job_id='job-1')
attempt = SimpleNamespace(id='attempt-1', attempt_number=1, status='SUCCEEDED',
    adapter_name='mpt-media-concat', output_relative_path='final/assembly-1/episode.mp4',
    metadata_json={{'duration_seconds': 3.2, 'resolution': '320x240', 'codec': 'h264',
                    'audio_stream': False, 'size_bytes': 1024, 'sha256': 'a' * 64}},
    created_at='2026-08-24T10:00:00+00:00', finished_at='2026-08-24T10:01:00+00:00', error_message=None)
class Production:
    def list_jobs(self, project_id): return [job]
    def validate_job_readiness(self, project_id, revision_id=None):
        return {{'ready': True, 'total_shots': 1, 'eligible_shots': 1, 'blocked_shots': 0,
                 'estimated_duration': 3.2, 'blocked_reasons': []}}
class Manifest:
    repository = None
    def list_assemblies(self, project_id, job_id): return [assembly]
class Runtime:
    def __init__(self, repository=None): pass
    def list_attempts(self, project_id, assembly_id): return [attempt]
    def resolve_output_path(self, project_id, assembly_id, attempt_id): return Path(r'{output_literal}')
page.current_project_or_stop = lambda: project
page.ProductionService = Production
page.FinalAssemblyService = Manifest
page.FinalAssemblyRuntimeService = Runtime
page.render()
"""
    ).run(timeout=30)
    assert not app.exception
    assert any("制作完成" in item.value for item in app.success)
    assert any(item.label == "后台导出 MP4" for item in app.button)
    assert not app.download_button
    assert any(item.label == "时长" and "00:03" in str(item.value) for item in app.metric)
