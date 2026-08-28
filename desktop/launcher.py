"""Loopback-only Streamlit launcher used by the AIDrama desktop shell.

PyWebView is the normal packaged window; a browser remains an explicit
development/emergency fallback when native WebView initialization genuinely
fails or ``--browser`` is requested.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
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


# In a PyInstaller onedir bundle, ``__file__`` points at the executable's
# bootstrap location while bundled Python/data modules live under
# ``sys._MEIPASS`` (``_internal``).  Use that root for the Streamlit script
# and child working directory; source-mode launches retain the repository root.
PROJECT_ROOT = Path(
    getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)
).resolve()
DEFAULT_MAIN = PROJECT_ROOT / "aidrama_studio" / "Main.py"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class DesktopLaunchError(RuntimeError):
    """Raised when the local Streamlit process cannot become healthy."""


class LauncherInstanceLock:
    """Cross-platform advisory lock held for the lifetime of the launcher.

    The lock file lives in the per-user data directory and is deliberately
    kept as a normal file so uninstall/upgrade operations never need to touch
    it.  Windows uses ``msvcrt.locking`` while POSIX smoke runs use
    ``fcntl.flock``.  Both locks are released automatically if the process
    exits, including an unhandled crash, so stale lock files are harmless.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path).resolve()
        self._handle: Any | None = None

    def acquire(self) -> "LauncherInstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            # msvcrt.locking requires at least one byte in the file and a
            # cursor positioned at the byte being locked.
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write("0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
        except (ImportError, OSError) as exc:
            handle.close()
            raise DesktopLaunchError(
                "AIDrama Studio is already running (or its launcher lock is unavailable)"
            ) from exc
        self._handle = handle
        return self

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            # Closing the descriptor still releases the OS lock.  Shutdown
            # must remain best-effort even if a filesystem was disconnected.
            pass
        finally:
            handle.close()

    def __enter__(self) -> "LauncherInstanceLock":
        return self.acquire()

    def __exit__(self, *_args: object) -> None:
        self.release()


_SECRET_IN_STARTUP_LOG = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*[^\s,;]+"
)


def _append_startup_log(path: Path | None, message: str) -> None:
    """Persist concise startup diagnostics without writing credentials."""

    if path is None:
        return
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        safe = _SECRET_IN_STARTUP_LOG.sub(r"\1=<redacted>", str(message))
        safe = " ".join(safe.replace("\r", " ").replace("\n", " ").split())
        with target.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {safe}\n")
    except OSError:
        # Diagnostics must never prevent the launcher from returning its real
        # startup status (for example when a profile is read-only).
        return


def _show_startup_error(message: str) -> None:
    """Show a concise native error for windowed frozen launches."""

    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    safe = _SECRET_IN_STARTUP_LOG.sub(r"\1=<redacted>", str(message))
    safe = " ".join(safe.replace("\r", " ").replace("\n", " ").split())
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, safe, "AIDrama Studio", 0x10)
    except Exception:
        # A missing user32 (or a non-interactive session) should not mask the
        # original startup failure, which is already persisted to the log.
        return
def validate_loopback_host(host: str) -> str:
    normalized = str(host).strip().lower()
    if normalized not in LOOPBACK_HOSTS:
        raise ValueError("AIDrama desktop launcher only permits loopback binding")
    return normalized


def configure_packaged_runtime_environment() -> Path | None:
    """Point frozen runtime configuration at the canonical user data root.

    PyInstaller places bundled resources under ``sys._MEIPASS`` (the
    ``_internal`` directory in an onedir build).  MPT's config loader is
    intentionally imported lazily, so the launcher can set its root before
    importing any service modules.  Only the no-secret example is copied on
    first start; existing user configuration is never overwritten.
    """

    if not getattr(sys, "frozen", False):
        return None
    # PyInstaller's windowed bootloader intentionally sets stdout/stderr to
    # ``None``. A few transitive libraries (including Streamlit and pywebview)
    # still emit diagnostics during startup, so give them a harmless sink
    # instead of allowing an otherwise healthy native launch to crash.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    explicit_data_root = os.environ.get("AIDRAMA_DATA_DIR", "").strip()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if explicit_data_root:
        # Test harnesses and managed deployments may provide an explicit
        # isolated root.  Preserve that contract while still forcing config
        # and database paths to the same directory.
        data_root = Path(explicit_data_root).expanduser()
    elif local_app_data:
        data_root = Path(local_app_data) / "AIDramaStudio"
    else:
        data_root = Path.home() / "AppData" / "Local" / "AIDramaStudio"
    data_root = data_root.resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    os.environ["MPT_CONFIG_DIR"] = str(data_root)
    os.environ["AIDRAMA_DATA_DIR"] = str(data_root)
    # Streamlit infers development mode from its frozen ``__file__`` path.
    # Explicitly disable that inference so the packaged child may use the
    # launcher-selected loopback port.
    os.environ.setdefault("STREAMLIT_GLOBAL_DEVELOPMENTMODE", "false")
    config_path = data_root / "config.toml"
    if not config_path.exists():
        bundled_template = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT)) / "config.example.toml"
        if bundled_template.is_file():
            shutil.copyfile(bundled_template, config_path)
    # imageio-ffmpeg normally resolves its own binary from package resources,
    # but PyInstaller onedir trees can place that resource under a different
    # internal directory.  Prefer an executable shipped in this bundle and
    # expose it through the application's existing explicit override.
    ffmpeg = _find_bundled_executable("ffmpeg.exe")
    if ffmpeg:
        os.environ["IMAGEIO_FFMPEG_EXE"] = str(ffmpeg)
    ffprobe = _find_bundled_executable("ffprobe.exe")
    if ffprobe:
        os.environ["AIDRAMA_FFPROBE_EXE"] = str(ffprobe)
    return data_root


def _find_bundled_executable(name: str) -> Path | None:
    """Return a bundled executable without consulting the machine PATH."""

    if not getattr(sys, "frozen", False):
        return None
    target = str(name).casefold()
    # imageio-ffmpeg's packaged binary is commonly named
    # ``ffmpeg-win64-v4.x.y.exe`` rather than simply ``ffmpeg.exe``.
    target_stem = Path(target).stem
    # Check common onedir locations first; the recursive fallback handles
    # imageio_ffmpeg's versioned ``binaries`` directory.
    roots = (PROJECT_ROOT, PROJECT_ROOT / "_internal")
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        candidates = [root / name, root / "ffmpeg" / name, root / "imageio_ffmpeg" / name]
        glob_pattern = f"{target_stem}*.exe" if target.endswith(".exe") else name
        candidates.extend(path for path in root.rglob(glob_pattern) if path.is_file())
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            candidate_name = resolved.name.casefold()
            if resolved.is_file() and (
                candidate_name == target
                or (candidate_name.endswith(".exe") and candidate_name.startswith(target_stem + "-"))
            ):
                return resolved
    return None


def _redirect_legacy_storage_to_appdata(data_root: Path | None) -> None:
    """Keep legacy ``app.services`` artifacts out of the install directory.

    The AIDrama Studio pages still reuse a few MoneyPrinterTurbo helpers whose
    historical ``storage_dir`` implementation derives paths from ``__file__``.
    In a frozen bundle that would point at the immutable install tree.  Patch
    only that helper at the packaging boundary; read-only ``resource_dir`` and
    model paths continue to resolve from the bundled application files.
    """

    if data_root is None:
        return
    try:
        from app.utils import utils as runtime_utils
    except Exception:
        return

    storage_root = Path(data_root).resolve() / "storage"

    def storage_dir(sub_dir: str = "", create: bool = False) -> str:
        target = storage_root / str(sub_dir) if sub_dir else storage_root
        if create:
            target.mkdir(parents=True, exist_ok=True)
        return str(target)

    def task_dir(sub_dir: str = "") -> str:
        target = storage_root / "tasks"
        if sub_dir:
            target /= str(sub_dir)
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    runtime_utils.storage_dir = storage_dir
    runtime_utils.task_dir = task_dir


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
    # Installed builds pass these paths from ``main``.  They remain optional
    # so source-mode callers and unit tests do not create files in a real
    # user's profile.
    instance_lock_path: Path | None = None
    startup_log_path: Path | None = None

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
        "--global.developmentMode=false",
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
    _instance_lock: LauncherInstanceLock | None = field(default=None, init=False, repr=False)
    _child_log_handle: Any | None = field(default=None, init=False, repr=False)
    port: int | None = field(default=None, init=False)
    url: str | None = field(default=None, init=False)

    @property
    def process(self) -> subprocess.Popen | None:
        return self._process

    def start(self) -> str:
        if self._process is not None and self._process.poll() is None:
            return str(self.url)
        if self.config.instance_lock_path is not None and self._instance_lock is None:
            self._instance_lock = LauncherInstanceLock(self.config.instance_lock_path)
            try:
                self._instance_lock.acquire()
            except DesktopLaunchError:
                self._instance_lock = None
                raise
        main_path = Path(self.config.main_path).resolve()
        if not main_path.is_file():
            self.stop()
            raise DesktopLaunchError(f"AIDrama entrypoint not found: {main_path}")
        if self.runner_host_factory is not None and self._runner_host is None:
            try:
                self._runner_host = self.runner_host_factory()
                self._runner_host.start()
            except Exception as exc:
                self._runner_host = None
                self.stop()
                raise DesktopLaunchError(f"无法启动 AIDrama 后台制作服务：{exc}") from exc
        port = select_safe_port(self.config.host, self.config.preferred_port, self.config.port_attempts)
        command = build_streamlit_command(self.config, port)
        child_stdout: Any = None
        if self.config.startup_log_path is not None:
            try:
                log_path = Path(self.config.startup_log_path)
                log_path.parent.mkdir(parents=True, exist_ok=True)
                self._child_log_handle = log_path.open("ab")
                child_stdout = self._child_log_handle
            except OSError:
                self._child_log_handle = None
        try:
            self._process = self.process_factory(
                command,
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=child_stdout,
                stderr=subprocess.STDOUT if child_stdout is not None else None,
            )
        except OSError as exc:
            if self._child_log_handle is not None:
                self._child_log_handle.close()
                self._child_log_handle = None
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
        try:
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
        finally:
            if self._child_log_handle is not None:
                self._child_log_handle.close()
                self._child_log_handle = None
            lock, self._instance_lock = self._instance_lock, None
            if lock is not None:
                lock.release()

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
            _append_startup_log(self.config.startup_log_path, f"startup failed: {exc}")
            _show_startup_error(f"AIDrama Studio 启动失败：{exc}")
            print(f"AIDrama desktop startup failed: {exc}", file=sys.stderr)
            return 1
        finally:
            self.stop()


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch the AIDrama Studio desktop shell")
    parser.add_argument("--version", action="store_true", help="print product version and build SHA")
    parser.add_argument("--port", type=int, default=8501, help="preferred loopback port")
    parser.add_argument("--host", default="127.0.0.1", help="loopback host only")
    parser.add_argument("--browser", action="store_true", help="force browser fallback")
    parser.add_argument("--smoke", action="store_true", help="start, health-check, and cleanly stop")
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    return parser.parse_args(argv)


def _print_build_version() -> int:
    """Expose the immutable package provenance to support operators."""

    candidates = []
    executable = getattr(sys, "executable", "")
    if executable:
        candidates.append(Path(executable).resolve().parent / "build-info.json")
    candidates.extend((PROJECT_ROOT / "build-info.json", PROJECT_ROOT.parent / "build-info.json"))
    for path in candidates:
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            print(
                f"AIDrama Studio {value.get('product_version', 'unknown')} "
                f"({value.get('delivery_head', 'unknown')})"
            )
            if os.name == "nt" and getattr(sys, "frozen", False):
                try:
                    import ctypes

                    ctypes.windll.user32.MessageBoxW(
                        None,
                        f"AIDrama Studio {value.get('product_version', 'unknown')}\n"
                        f"Build SHA: {value.get('delivery_head', 'unknown')}",
                        "AIDrama Studio version",
                        0x40,
                    )
                except Exception:
                    pass
            return 0
    print("AIDrama Studio version metadata unavailable", file=sys.stderr)
    return 2


def _run_streamlit_child(argv: Sequence[str]) -> int:
    """Run Streamlit from a frozen launcher child process.

    This path is only selected by the explicit ``--streamlit-child`` marker
    emitted by :func:`build_streamlit_command` in a PyInstaller bundle.  It is
    lazy so normal source launches do not import Streamlit in the shell
    process, and it keeps the desktop executable as the sole package entry
    point.
    """

    data_root = os.environ.get("AIDRAMA_DATA_DIR", "").strip()
    _redirect_legacy_storage_to_appdata(Path(data_root) if data_root else None)
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
    data_root = configure_packaged_runtime_environment()
    if raw_argv and raw_argv[0] == "--streamlit-child":
        return _run_streamlit_child(raw_argv[1:])
    args = _parse_args(raw_argv)
    if args.version:
        return _print_build_version()
    from desktop.background import DesktopBackgroundRunnerHost

    launcher = DesktopLauncher(
        LauncherConfig(
            host=args.host,
            preferred_port=args.port,
            startup_timeout=args.startup_timeout,
            instance_lock_path=(data_root / "launcher.lock") if data_root else None,
            startup_log_path=(data_root / "logs" / "launcher.log") if data_root else None,
        ),
        runner_host_factory=DesktopBackgroundRunnerHost,
    )
    if args.smoke:
        try:
            print(launcher.start())
            return 0
        except DesktopLaunchError as exc:
            _append_startup_log(launcher.config.startup_log_path, f"smoke failed: {exc}")
            print(f"AIDrama desktop smoke failed: {exc}", file=sys.stderr)
            return 1
        finally:
            launcher.stop()
    return launcher.run(browser_fallback=args.browser)


if __name__ == "__main__":
    raise SystemExit(main())
