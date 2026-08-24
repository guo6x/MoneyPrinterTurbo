from __future__ import annotations

from pathlib import Path

import pytest

from aidrama_studio.branding import BRAND, get_brand_config
from aidrama_studio.services.provider_readiness import (
    ProviderReadinessService,
    ReadinessState,
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
        env={"DASHSCOPE_API_KEY": secret, "WAN_VIDEO_MODEL": "test-model"},
        llm_status=lambda: (False, "API Key 尚未配置"),
    )
    snapshot = service.snapshot()
    assert snapshot["VIDEO_GENERATIVE"]["state"] == ReadinessState.READY.value
    assert secret not in repr(snapshot)
    assert snapshot["IMAGE"]["state"] == ReadinessState.UNAVAILABLE.value
    assert snapshot["VISION"]["state"] == ReadinessState.UNAVAILABLE.value


def test_launcher_rejects_non_loopback_binding():
    with pytest.raises(ValueError, match="loopback"):
        validate_loopback_host("0.0.0.0")
    with pytest.raises(ValueError, match="loopback"):
        LauncherConfig(host="192.168.1.2")


def test_launcher_command_is_explicit_loopback_command(tmp_path: Path):
    config = LauncherConfig(main_path=tmp_path / "Main.py")
    command = build_streamlit_command(config, 8511)
    assert command[:4] == [config.python_executable, "-m", "streamlit", "run"]
    assert "--server.address" in command
    assert command[command.index("--server.address") + 1] == "127.0.0.1"
    assert command[command.index("--server.port") + 1] == "8511"
    assert health_url("127.0.0.1", 8511).endswith("/_stcore/health")


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
