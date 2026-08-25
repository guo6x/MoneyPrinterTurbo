from __future__ import annotations

from aidrama_studio.services.ai_capabilities import MPTLLMProvider


def test_explicit_empty_llm_snapshot_never_falls_back_to_live_global(monkeypatch):
    called = False

    def live_snapshot():
        nonlocal called
        called = True
        return {"llm_provider": "moonshot"}

    monkeypatch.setattr(
        "aidrama_studio.services.ai_capabilities.snapshot_llm_config",
        live_snapshot,
    )
    provider = MPTLLMProvider({})

    assert provider._config_snapshot == {}
    assert called is False
