from __future__ import annotations

from pathlib import Path
import sys
import threading

import pytest

from aidrama_studio.branding import get_brand_config
from aidrama_studio.services.provider_readiness import (
    ProviderReadinessService,
    ReadinessState,
)
from aidrama_studio.services.ai_capabilities import (
    CapabilityKind,
    CapabilityRegistry,
    CapabilityStatus,
)
from desktop.launcher import (
    DesktopLaunchError,
    DesktopLauncher,
    LauncherConfig,
    build_streamlit_command,
    health_url,
    select_safe_port,
    validate_loopback_host,
    wait_for_health,
)
from desktop.build import DESKTOP_ENTRYPOINT, build_command, runtime_data_args
from desktop.background import DesktopBackgroundError, DesktopBackgroundRunnerHost
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository


def test_brand_config_has_replaceable_mark_without_upstream_ui_name(monkeypatch):
    monkeypatch.delenv("AIDRAMA_LOGO_PATH", raising=False)
    brand = get_brand_config()
    assert brand.product_name == "AIDrama Studio"
    assert brand.short_name == "AIDrama"
    assert brand.logo_exists
    assert "MoneyPrinterTurbo" not in brand.product_name
    assert brand.as_public_dict()["logo_exists"] is True


def test_provider_readiness_never_returns_secret_values():
    secret = "test-secret-do-not-render"
    service = ProviderReadinessService(
        env={
            "DASHSCOPE_API_KEY": secret,
            "WAN_VIDEO_MODEL": "test-model",
            "AIDRAMA_ALLOW_PAID_LIVE_TESTS": "1",
        },
        llm_status=lambda: (False, "API Key 尚未配置"),
    )
    snapshot = service.snapshot()
    assert snapshot["VIDEO_GENERATIVE"]["state"] == ReadinessState.READY.value
    assert secret not in repr(snapshot)
    assert snapshot["IMAGE"]["state"] == ReadinessState.UNAVAILABLE.value
    assert snapshot["VISION"]["state"] == ReadinessState.UNAVAILABLE.value


def test_provider_readiness_keeps_production_and_explicit_env_resolution_distinct(
    monkeypatch,
):
    import aidrama_studio.services.provider_readiness as readiness_module
    from aidrama_studio.services.ai_capabilities import CapabilityRegistry

    calls = []
    registry = CapabilityRegistry([])

    def fake_registry(*args, **kwargs):
        calls.append((args, kwargs))
        return registry

    monkeypatch.setattr(readiness_module, "default_capability_registry", fake_registry)

    readiness_module.ProviderReadinessService()._capability_registry()
    readiness_module.ProviderReadinessService(env={"AIDRAMA_TEST": "1"})._capability_registry()

    assert calls[0] == ((), {})
    assert calls[1] == ((), {"env": {"AIDRAMA_TEST": "1"}})


def test_provider_readiness_fails_closed_on_contradictory_runtime_status():
    class BrokenImageProvider:
        capability = CapabilityKind.IMAGE
        provider_name = "BROKEN_IMAGE"

        @property
        def status(self):
            return CapabilityStatus(
                CapabilityKind.IMAGE,
                self.provider_name,
                True,
                "invalid endpoint configuration",
                {
                    "model": "broken-image-v1",
                    "deployment_region": "INTERNATIONAL",
                    "endpoint_class": "BROKEN_PUBLIC",
                    "endpoint_profile_id": (
                        "runtime:IMAGE:BROKEN_IMAGE:BROKEN_PUBLIC"
                    ),
                    "configured": True,
                    "credential_present": True,
                    "provider_constraints_valid": False,
                },
                configured=True,
            )

    snapshot = ProviderReadinessService(
        registry=CapabilityRegistry([BrokenImageProvider()])
    ).snapshot()
    assert snapshot["IMAGE"]["state"] == ReadinessState.ERROR.value
    assert snapshot["IMAGE"]["ready"] is False


def test_launcher_rejects_non_loopback_binding():
    with pytest.raises(ValueError, match="loopback"):
        validate_loopback_host("0.0.0.0")
    with pytest.raises(ValueError, match="loopback"):
        LauncherConfig(host="192.168.1.2")


def test_launcher_normalizes_safe_loopback_aliases():
    assert LauncherConfig(host="  LOCALHOST ").host == "localhost"


def test_launcher_command_is_explicit_loopback_command(tmp_path: Path):
    config = LauncherConfig(main_path=tmp_path / "Main.py")
    command = build_streamlit_command(config, 8511)
    assert command[:4] == [config.python_executable, "-m", "streamlit", "run"]
    assert "--server.address" in command
    assert command[command.index("--server.address") + 1] == "127.0.0.1"
    assert command[command.index("--server.port") + 1] == "8511"
    assert health_url("127.0.0.1", 8511).endswith("/_stcore/health")


def test_frozen_launcher_reenters_streamlit_child_mode(monkeypatch, tmp_path: Path):
    config = LauncherConfig(main_path=tmp_path / "Main.py")
    monkeypatch.setattr("desktop.launcher.sys.frozen", True, raising=False)
    command = build_streamlit_command(config, 8511)
    assert command[:2] == [config.python_executable, "--streamlit-child"]
    assert command[2] == "run"
    assert command[command.index("--server.address") + 1] == "127.0.0.1"


def test_pywebview_unavailable_opens_browser_fallback(monkeypatch):
    process = _LifecycleProcess([None])
    launcher = _running_launcher(process)
    opened = []
    monkeypatch.setitem(sys.modules, "webview", None)
    monkeypatch.setattr(
        "desktop.launcher.webbrowser.open",
        lambda url, new=0: opened.append((url, new)) or True,
    )

    assert launcher.open_window(prefer_webview=True) == "browser-fallback"
    assert opened == [(launcher.url, 1)]


class _HealthyResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b"ok"


def test_wait_for_health_accepts_streamlit_ok_response():
    assert wait_for_health("http://127.0.0.1:8501/_stcore/health", timeout=0.2, interval=0.01, opener=lambda *args, **kwargs: _HealthyResponse())


class _FakeProcess:
    def __init__(self, *args, **kwargs):
        self.command = args[0] if args else None
        self.kwargs = kwargs
        self.return_code = None
        self.terminated = False

    def poll(self):
        return self.return_code

    def terminate(self):
        self.terminated = True
        self.return_code = 0

    def wait(self, timeout=None):
        return self.return_code


class _LifecycleProcess:
    def __init__(self, polls: list[int | None] | None = None) -> None:
        self.polls = list(polls or [None])
        self.terminated = False

    def poll(self):
        if len(self.polls) > 1:
            return self.polls.pop(0)
        return self.polls[0]

    def terminate(self):
        self.terminated = True
        self.polls = [0]

    def kill(self):
        self.polls = [-9]

    def wait(self, timeout=None):
        return self.poll()


def test_launcher_stops_child_after_health(monkeypatch, tmp_path: Path):
    main = tmp_path / "Main.py"
    main.write_text("# test entrypoint", encoding="utf-8")
    process = _FakeProcess()
    monkeypatch.setattr("desktop.launcher.select_safe_port", lambda *args: 8512)
    monkeypatch.setattr("desktop.launcher.wait_for_health", lambda *args, **kwargs: True)
    launcher = DesktopLauncher(
        LauncherConfig(main_path=main),
        process_factory=lambda *args, **kwargs: process,
    )
    assert launcher.start() == "http://127.0.0.1:8512"
    launcher.stop()
    assert process.terminated


def test_launcher_cleans_up_when_health_timeout(monkeypatch, tmp_path: Path):
    main = tmp_path / "Main.py"
    main.write_text("# test entrypoint", encoding="utf-8")
    process = _FakeProcess()
    monkeypatch.setattr("desktop.launcher.select_safe_port", lambda *args: 8513)
    monkeypatch.setattr("desktop.launcher.wait_for_health", lambda *args, **kwargs: False)
    launcher = DesktopLauncher(
        LauncherConfig(main_path=main),
        process_factory=lambda *args, **kwargs: process,
    )
    with pytest.raises(DesktopLaunchError, match="未在限定时间内就绪"):
        launcher.start()
    assert process.terminated


def test_select_safe_port_returns_loopback_port():
    port = select_safe_port(preferred_port=0)
    assert 1 <= port <= 65535


def _running_launcher(process: _LifecycleProcess) -> DesktopLauncher:
    launcher = DesktopLauncher(LauncherConfig(health_interval=0.01))
    launcher._process = process
    launcher.url = "http://127.0.0.1:8511"
    launcher.port = 8511
    return launcher


def test_default_webview_fallback_keeps_child_alive_until_child_exit(monkeypatch):
    process = _LifecycleProcess([None, None, 0])
    launcher = _running_launcher(process)
    monkeypatch.setattr("desktop.launcher.time.sleep", lambda *_args: None)
    monkeypatch.setattr(launcher, "start", lambda: launcher.url)
    monkeypatch.setitem(sys.modules, "webview", None)
    opened = []
    monkeypatch.setattr(
        "desktop.launcher.webbrowser.open",
        lambda url, new=0: opened.append((url, new)) or True,
    )

    assert launcher.run() == 0
    assert launcher.process is None
    # The child exited naturally; cleanup is still idempotent.
    assert not process.terminated
    assert opened == [(launcher.url, 1)]


def test_browser_fallback_keyboard_interrupt_stops_child(monkeypatch):
    process = _LifecycleProcess([None])
    launcher = _running_launcher(process)
    monkeypatch.setattr(launcher, "start", lambda: launcher.url)
    monkeypatch.setattr(launcher, "open_window", lambda **_kwargs: "browser-fallback")

    def interrupt():
        raise KeyboardInterrupt

    monkeypatch.setattr(launcher, "_wait_for_browser_session", interrupt)
    assert launcher.run() == 0
    assert process.terminated
    assert launcher.process is None


def test_webview_lifecycle_returns_then_stops_child(monkeypatch):
    process = _LifecycleProcess([None])
    launcher = _running_launcher(process)
    monkeypatch.setattr(launcher, "start", lambda: launcher.url)
    monkeypatch.setattr(launcher, "open_window", lambda **_kwargs: "webview")
    wait_called = False

    def unexpected_wait():
        nonlocal wait_called
        wait_called = True

    monkeypatch.setattr(launcher, "_wait_for_browser_session", unexpected_wait)
    assert launcher.run() == 0
    assert process.terminated
    assert not wait_called


def test_health_probe_interruption_cleans_child(monkeypatch, tmp_path: Path):
    main = tmp_path / "Main.py"
    main.write_text("# test entrypoint", encoding="utf-8")
    process = _LifecycleProcess([None])
    launcher = DesktopLauncher(
        LauncherConfig(main_path=main), process_factory=lambda *_args, **_kwargs: process
    )
    monkeypatch.setattr("desktop.launcher.select_safe_port", lambda *_args: 8512)

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("desktop.launcher.wait_for_health", interrupt)
    with pytest.raises(KeyboardInterrupt):
        launcher.start()
    assert process.terminated
    assert launcher.process is None


def test_pyinstaller_targets_desktop_launcher_and_runtime_assets(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("desktop.build.importlib.util.find_spec", lambda _name: object())
    command = build_command(output_dir=tmp_path / "dist")

    assert command[-1] == str(DESKTOP_ENTRYPOINT)
    assert command[-1].endswith(str(Path("desktop") / "launcher.py"))
    assert str(Path("aidrama_studio") / "Main.py") not in command[-1]
    assert "--collect-all" in command
    assert command[command.index("--collect-all") + 1] == "streamlit"
    assert command.count("--collect-submodules") >= 2
    data_args = runtime_data_args()
    assert "--add-data" in data_args
    assert any("styles.css" in value for value in data_args)
    assert any("assets" in value for value in data_args)
    assert any("LICENSE" in value for value in data_args)
    assert any("NOTICE" in value for value in data_args)
    assert any("THIRD_PARTY_NOTICES.md" in value for value in data_args)


def test_build_entrypoint_exists_and_is_loopback_launcher():
    assert DESKTOP_ENTRYPOINT.is_file()
    source = DESKTOP_ENTRYPOINT.read_text(encoding="utf-8")
    assert "DesktopLauncher" in source
    assert "validate_loopback_host" in source


def test_desktop_background_runner_survives_ui_reruns_and_stops_cleanly(tmp_path):
    repository = ProjectRepository(
        DatabasePaths(
            tmp_path / "data" / "aidrama.db",
            tmp_path / "data" / "projects",
            tmp_path / "data" / "archived",
        )
    )
    cycle = threading.Event()
    instances = []

    class FakeRunner:
        def __init__(self, repo, **kwargs):
            self.repository = repo
            self.worker_factory = kwargs["worker_factory"]
            self.reconciled = []
            instances.append(self)

        def reconcile(self, project_id):
            self.reconciled.append(project_id)

        def run_once(self):
            cycle.set()
            return []

    host = DesktopBackgroundRunnerHost(
        repository,
        interval_seconds=0.01,
        runner_factory=FakeRunner,
    )
    host.start()
    assert cycle.wait(1)
    assert host.running
    # Calling start again models a Streamlit rerun; no duplicate runner is
    # created for the same desktop owner.
    host.start()
    assert len(instances) == 1
    host.stop(timeout_seconds=1)
    assert not host.running


def test_desktop_data_directory_has_one_writable_owner(tmp_path):
    repository = ProjectRepository(
        DatabasePaths(
            tmp_path / "data" / "aidrama.db",
            tmp_path / "data" / "projects",
            tmp_path / "data" / "archived",
        )
    )

    class IdleRunner:
        def __init__(self, *_args, **_kwargs):
            pass

        def reconcile(self, _project_id):
            return []

        def run_once(self):
            return []

    first = DesktopBackgroundRunnerHost(repository, interval_seconds=0.1, runner_factory=IdleRunner)
    second = DesktopBackgroundRunnerHost(repository, interval_seconds=0.1, runner_factory=IdleRunner)
    first.start()
    try:
        with pytest.raises(DesktopBackgroundError, match="另一个桌面实例"):
            second.start()
    finally:
        first.stop(timeout_seconds=1)
