"""Loopback-only Streamlit launcher used by the optional desktop shell.

PyWebView is intentionally optional.  The same launcher can open the local
application in a browser for development/fallback, while a packaged build can
inject the PyWebView module without changing process or security behavior.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MAIN = PROJECT_ROOT / "aidrama_studio" / "Main.py"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class DesktopLaunchError(RuntimeError):
    """Raised when the local Streamlit process cannot become healthy."""


def validate_loopback_host(host: str) -> str:
    normalized = str(host).strip().lower()
    if normalized not in LOOPBACK_HOSTS:
        raise ValueError("AIDrama desktop launcher only permits loopback binding")
    return normalized


@dataclass(frozen=True)
class LauncherConfig:
    """Safe local launch settings.

    ``host`` is validated in :meth:`__post_init__`; callers cannot accidentally
    expose the application to a LAN by passing a wildcard address.
    """

    main_path: Path = DEFAULT_MAIN
    python_executable: str = sys.executable
    host: str = "127.0.0.1"
    preferred_port: int = 8501
    port_attempts: int = 20
    startup_timeout: float = 30.0
    health_interval: float = 0.2
    streamlit_module: str = "streamlit"

    def __post_init__(self) -> None:
        object.__setattr__(self, "host", validate_loopback_host(self.host))
        if not 0 <= int(self.preferred_port) <= 65535:
            raise ValueError("preferred_port must be between 0 and 65535")
        if int(self.port_attempts) < 1:
            raise ValueError("port_attempts must be positive")
        if self.startup_timeout <= 0 or self.health_interval <= 0:
            raise ValueError("startup_timeout and health_interval must be positive")


def is_port_available(host: str, port: int) -> bool:
    """Return whether a TCP port can be bound on the loopback interface."""

    host = validate_loopback_host(host)
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
        except OSError:
            return False
    return True


def select_safe_port(host: str = "127.0.0.1", preferred_port: int = 8501, attempts: int = 20) -> int:
    """Select a currently free loopback port, preferring Streamlit's default."""

    host = validate_loopback_host(host)
    preferred_port = int(preferred_port)
    if preferred_port and is_port_available(host, preferred_port):
        return preferred_port
    if preferred_port:
        for offset in range(1, max(1, int(attempts)) + 1):
            candidate = preferred_port + offset
            if candidate <= 65535 and is_port_available(host, candidate):
                return candidate
    # Binding port zero asks the OS for a free ephemeral port and avoids a
    # collision when all preferred candidates are occupied.
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def build_streamlit_command(config: LauncherConfig, port: int) -> list[str]:
    """Build the explicit local Streamlit command used by the launcher."""

    host = validate_loopback_host(config.host)
    # A PyInstaller executable is not a Python interpreter and cannot execute
    # ``-m streamlit`` directly.  The frozen launcher includes Streamlit, so it
    # can re-enter itself in a dedicated child mode while preserving the same
    # explicit script/loopback arguments used by source checkouts.
    if getattr(sys, "frozen", False):
        interpreter = [config.python_executable, "--streamlit-child"]
    else:
        interpreter = [config.python_executable, "-m", config.streamlit_module]
    return [
        *interpreter,
        "run",
        str(Path(config.main_path).resolve()),
        "--server.address",
        host,
        "--server.port",
        str(int(port)),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]


def health_url(host: str, port: int) -> str:
    host = validate_loopback_host(host)
    display_host = "[::1]" if host == "::1" else host
    return f"http://{display_host}:{int(port)}/_stcore/health"


def wait_for_health(
    url: str,
    *,
    timeout: float = 30.0,
    interval: float = 0.2,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> bool:
    """Wait for Streamlit's health endpoint, handling startup races."""

    deadline = time.monotonic() + float(timeout)
    while time.monotonic() < deadline:
        try:
            with opener(url, timeout=min(2.0, max(0.1, deadline - time.monotonic()))) as response:
                body = response.read().decode("utf-8", errors="replace").strip().lower()
                if getattr(response, "status", 200) == 200 and body in {"ok", "healthy", ""}:
                    return True
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            pass
        time.sleep(min(float(interval), max(0.01, deadline - time.monotonic())))
    return False


@dataclass
class DesktopLauncher:
    """Manage the local Streamlit child process and optional desktop window."""

    config: LauncherConfig = field(default_factory=LauncherConfig)
    process_factory: Callable[..., subprocess.Popen] = subprocess.Popen
    runner_host_factory: Callable[[], Any] | None = None
    _process: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _runner_host: Any | None = field(default=None, init=False, repr=False)
    port: int | None = field(default=None, init=False)
    url: str | None = field(default=None, init=False)

    @property
    def process(self) -> subprocess.Popen | None:
        return self._process

    def start(self) -> str:
        if self._process is not None and self._process.poll() is None:
            return str(self.url)
        main_path = Path(self.config.main_path).resolve()
        if not main_path.is_file():
            raise DesktopLaunchError(f"AIDrama entrypoint not found: {main_path}")
        if self.runner_host_factory is not None and self._runner_host is None:
            try:
                self._runner_host = self.runner_host_factory()
                self._runner_host.start()
            except Exception as exc:
                self._runner_host = None
                raise DesktopLaunchError(f"无法启动 AIDrama 后台制作服务：{exc}") from exc
        port = select_safe_port(self.config.host, self.config.preferred_port, self.config.port_attempts)
        command = build_streamlit_command(self.config, port)
        try:
            self._process = self.process_factory(
                command,
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=None,
                stderr=None,
            )
        except OSError as exc:
            self._stop_runner_host()
            raise DesktopLaunchError(f"无法启动 AIDrama 本地服务：{exc}") from exc
        self.port = port
        display_host = "[::1]" if self.config.host == "::1" else self.config.host
        self.url = f"http://{display_host}:{port}"
        try:
            healthy = wait_for_health(
                health_url(self.config.host, port),
                timeout=self.config.startup_timeout,
                interval=self.config.health_interval,
            )
        except BaseException:
            # ``start`` is also a public API used by smoke checks.  Ensure a
            # child cannot survive an interrupted health probe even when the
            # caller does not enter ``run`` (whose ``finally`` is a second
            # line of defence).
            self.stop()
            raise
        if not healthy:
            return_code = self._process.poll()
            self.stop()
            suffix = f"（进程退出码 {return_code}）" if return_code is not None else ""
            raise DesktopLaunchError(f"AIDrama 本地服务未在限定时间内就绪{suffix}")
        return self.url

    def _wait_for_browser_session(self) -> None:
        """Keep a browser-fallback child alive until it exits or is interrupted.

        Opening a browser is asynchronous and returns immediately.  The
        launcher therefore must remain the owner of the local Streamlit child
        instead of returning into ``finally`` and terminating it.  Polling the
        child also lets a crashed/closed child end the fallback naturally.
        ``KeyboardInterrupt`` is intentionally handled by :meth:`run` so this
        helper remains useful in tests with a deterministic fake clock.
        """

        while True:
            process = self.process
            if process is None or process.poll() is not None:
                return
            time.sleep(max(0.05, float(self.config.health_interval)))

    def stop(self) -> None:
        self._stop_runner_host()
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def _stop_runner_host(self) -> None:
        host, self._runner_host = self._runner_host, None
        if host is None:
            return
        try:
            host.stop()
        except Exception as exc:
            print(f"AIDrama background runner shutdown warning: {exc}", file=sys.stderr)

    def open_window(self, *, prefer_webview: bool = True) -> str:
        if not self.url:
            raise DesktopLaunchError("本地服务尚未启动")
        if prefer_webview:
            try:
                import webview  # type: ignore[import-not-found]

                webview.create_window("AIDrama Studio", self.url)
                webview.start()
                return "webview"
            except ImportError:
                pass
            except Exception as exc:
                # A packaged WebView can fail to initialize on a machine with
                # missing GUI runtime support. Keep the local server usable via
                # the documented browser fallback instead of leaking a trace.
                print(f"AIDrama WebView unavailable; using browser fallback: {exc}", file=sys.stderr)
        webbrowser.open(self.url, new=1)
        return "browser-fallback"

    def run(self, *, browser_fallback: bool = False) -> int:
        try:
            self.start()
            window_mode = self.open_window(prefer_webview=not browser_fallback)
            # ``open_window`` transparently falls back when PyWebView is not
            # installed or cannot initialize.  Handle that result exactly like
            # an explicit ``--browser`` invocation; otherwise ``finally``
            # would immediately stop the healthy local service.
            if window_mode == "browser-fallback":
                self._wait_for_browser_session()
            return 0
        except KeyboardInterrupt:
            return 0
        except DesktopLaunchError as exc:
            print(f"AIDrama desktop startup failed: {exc}", file=sys.stderr)
            return 1
        finally:
            self.stop()


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch the AIDrama Studio desktop shell")
    parser.add_argument("--port", type=int, default=8501, help="preferred loopback port")
    parser.add_argument("--host", default="127.0.0.1", help="loopback host only")
    parser.add_argument("--browser", action="store_true", help="force browser fallback")
    parser.add_argument("--smoke", action="store_true", help="start, health-check, and cleanly stop")
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    return parser.parse_args(argv)


def _run_streamlit_child(argv: Sequence[str]) -> int:
    """Run Streamlit from a frozen launcher child process.

    This path is only selected by the explicit ``--streamlit-child`` marker
    emitted by :func:`build_streamlit_command` in a PyInstaller bundle.  It is
    lazy so normal source launches do not import Streamlit in the shell
    process, and it keeps the desktop executable as the sole package entry
    point.
    """

    from streamlit.web.cli import main as streamlit_main

    previous_argv = sys.argv
    try:
        sys.argv = ["streamlit", *argv]
        result = streamlit_main()
        return int(result or 0)
    finally:
        sys.argv = previous_argv


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "--streamlit-child":
        return _run_streamlit_child(raw_argv[1:])
    args = _parse_args(raw_argv)
    from desktop.background import DesktopBackgroundRunnerHost

    launcher = DesktopLauncher(
        LauncherConfig(host=args.host, preferred_port=args.port, startup_timeout=args.startup_timeout),
        runner_host_factory=DesktopBackgroundRunnerHost,
    )
    if args.smoke:
        try:
            print(launcher.start())
            return 0
        except DesktopLaunchError as exc:
            print(f"AIDrama desktop smoke failed: {exc}", file=sys.stderr)
            return 1
        finally:
            launcher.stop()
    return launcher.run(browser_fallback=args.browser)


if __name__ == "__main__":
    raise SystemExit(main())
