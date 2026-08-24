"""Windows user-scoped credential storage with no plaintext fallback."""

from __future__ import annotations

import base64
import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path
from typing import Mapping


class CredentialStoreError(RuntimeError):
    pass


class WindowsCredentialStore:
    """Protect provider credentials with Windows DPAPI.

    The encrypted blob is user/machine scoped and never enters SQLite,
    snapshots, logs, provider requests or exported projects.
    """

    def __init__(self, root: Path | None = None) -> None:
        if os.name != "nt":
            raise CredentialStoreError("Windows DPAPI 仅在 Windows 桌面运行时可用")
        data_root = Path(root) if root is not None else Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AIDramaStudio"
        self.path = data_root / "credentials.dpapi"

    def set(self, provider_id: str, secret: str) -> None:
        if not provider_id or not isinstance(secret, str) or not secret:
            raise CredentialStoreError("provider credential 不能为空")
        values = self._read()
        values[provider_id] = secret
        self._write(values)

    def get(self, provider_id: str) -> str | None:
        return self._read().get(provider_id)

    def delete(self, provider_id: str) -> None:
        values = self._read()
        if provider_id in values:
            del values[provider_id]
            self._write(values)

    def configured(self, provider_id: str) -> bool:
        return bool(self.get(provider_id))

    def configured_providers(self) -> tuple[str, ...]:
        return tuple(sorted(self._read()))

    def _read(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        try:
            payload = base64.b64decode(self.path.read_bytes(), validate=True)
            raw = self._unprotect(payload)
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict) or any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()):
                raise ValueError("credential store structure invalid")
            return dict(value)
        except Exception as exc:
            raise CredentialStoreError("credential store cannot be opened") from exc

    def _write(self, values: Mapping[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encrypted = base64.b64encode(self._protect(json.dumps(dict(values), ensure_ascii=False, sort_keys=True).encode("utf-8")))
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(encrypted)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _protect(data: bytes) -> bytes:
        return _dpapi(data, protect=True)

    @staticmethod
    def _unprotect(data: bytes) -> bytes:
        return _dpapi(data, protect=False)


class CredentialReadinessService:
    """Expose safe setup/readiness information without secret values."""

    def __init__(self, store: WindowsCredentialStore | None = None) -> None:
        self.store = store

    def status(self, provider_ids: tuple[str, ...] | list[str]) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for provider_id in provider_ids:
            configured = bool(self.store and self.store.configured(provider_id))
            result[provider_id] = {"configured": configured, "secret": "<redacted>" if configured else None}
        return result


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    raw = ctypes.create_string_buffer(data)
    source = _DATA_BLOB(len(data), ctypes.cast(raw, ctypes.POINTER(ctypes.c_char)))
    destination = _DATA_BLOB()
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    function.argtypes = [ctypes.POINTER(_DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(_DATA_BLOB), wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(_DATA_BLOB)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), "AIDrama Studio provider credentials", None, None, None, 0x01, ctypes.byref(destination)):
        raise CredentialStoreError("Windows credential protection failed")
    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        kernel32.LocalFree(destination.pbData)


__all__ = ["CredentialReadinessService", "CredentialStoreError", "WindowsCredentialStore"]
