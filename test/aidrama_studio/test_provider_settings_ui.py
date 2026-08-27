from __future__ import annotations

from streamlit.testing.v1 import AppTest


def test_settings_model_scheme_renders_presets_five_capabilities_and_safe_metadata():
    app = AppTest.from_string(
        """
from types import SimpleNamespace
from aidrama_studio.domain import ProviderPreset
from aidrama_studio.pages import settings as page

labels = {
    'LLM': 'LLM',
    'IMAGE': 'IMAGE',
    'VIDEO_GENERATIVE': 'VIDEO',
    'VISION': 'VISION',
    'TTS': 'TTS',
}

def profile(capability):
    return SimpleNamespace(
        id='profile-'+capability,
        endpoint_profile_id='endpoint-'+capability,
        provider_id='SAFE_'+labels[capability],
        model_id='model-'+capability.lower(),
        deployment_region=SimpleNamespace(value='MAINLAND_CHINA'),
        endpoint_class='CN_PUBLIC',
        profile={'maximum_duration_seconds': 15},
    )

class Resolution:
    def __init__(self, capability):
        self.profile = profile(capability)
        self.capability = capability
    def as_public_dict(self):
        return {
            'capability': self.capability,
            'preset': 'MAINLAND',
            'state': 'CONFIGURED',
            'source': 'GLOBAL_DEFAULT',
            'provider_id': self.profile.provider_id,
            'model_id': self.profile.model_id,
            'endpoint_profile_id': self.profile.endpoint_profile_id,
            'deployment_region': 'MAINLAND_CHINA',
            'endpoint_class': 'CN_PUBLIC',
            'configured': True,
            'available': False,
            'verified': False,
            'detail': 'paid live authorization is required',
        }

class Service:
    def get_settings(self, project_id=None):
        return SimpleNamespace(preset=ProviderPreset.MAINLAND, selections={})
    def inventory(self, project_id, capability):
        return (profile(capability.value),)
    def resolve(self, project_id, capability):
        return Resolution(capability.value)
    def save_settings(self, **kwargs):
        raise AssertionError('not clicked')

page._render_provider_model_settings(Service(), project_id='project-1')
"""
    ).run()

    assert not app.exception
    assert any(radio.label == "模型方案" for radio in app.radio)
    preset = next(radio for radio in app.radio if radio.label == "模型方案")
    assert preset.options == ["中国大陆", "国际", "自定义"]
    rendered = "\n".join(
        str(element.value)
        for collection in (app.markdown, app.caption, app.info, app.warning)
        for element in collection
    )
    for label in ("文本生成", "参考图生成", "视频生成", "画面分析", "配音"):
        assert label in rendered
    assert "MAINLAND_CHINA" in rendered
    # The fixture deliberately models configured profiles whose runtime
    # prerequisites are unavailable (``available=False``).  The normal-user
    # surface must therefore avoid a false-ready claim and show the actionable
    # three-state label instead.
    assert "需要配置" in rendered
    assert "已配置" not in rendered
    assert "未验证" in rendered
    assert "只影响新 RuntimePlan" in rendered
    assert "secret-value" not in rendered
    assert "API Key" not in rendered


def test_normal_readiness_surface_uses_three_human_states_for_five_capabilities():
    app = AppTest.from_string(
        """
from aidrama_studio.pages import _shared
import aidrama_studio.services.provider_readiness as readiness_module

class Readiness:
    def snapshot(self, *, project_id=None):
        assert project_id == 'project-1'
        return {
            'LLM': {'state': 'READY', 'ready': True, 'detail': 'configured'},
            'IMAGE': {'state': 'UNAVAILABLE', 'ready': False, 'detail': 'credential missing'},
            'VIDEO_GENERATIVE': {'state': 'ERROR', 'ready': False, 'detail': 'invalid endpoint'},
            'VISION': {'state': 'READY', 'ready': True, 'detail': 'configured'},
            'TTS': {'state': 'UNAVAILABLE', 'ready': False, 'detail': 'region missing'},
        }

_original_readiness = readiness_module.ProviderReadinessService
readiness_module.ProviderReadinessService = Readiness
try:
    _shared.render_ai_readiness(project_id='project-1', compact=True)
finally:
    # AppTest executes this snippet in the importing interpreter.  Restore the
    # module seam so later tests observe the production readiness service.
    readiness_module.ProviderReadinessService = _original_readiness
"""
    ).run()

    assert not app.exception
    metrics = {item.label: item.value for item in app.metric}
    assert metrics == {
        "文本生成": "已配置",
        "参考图生成": "需要配置",
        "视频生成": "配置有误",
        "画面分析": "已配置",
        "配音": "需要配置",
    }


def test_runtime_credential_uses_direct_submit_form_without_enter_key():
    app = AppTest.from_string(
        """
from types import SimpleNamespace
import streamlit as st
from aidrama_studio.pages import settings as page

class Store:
    def __init__(self):
        self.values = {}
    def configured(self, key):
        return bool(self.values.get(key))
    def configured_providers(self):
        return tuple(self.values)
    def set(self, key, value):
        self.values[key] = value
    def delete(self, key):
        self.values.pop(key, None)

store = st.session_state.setdefault('_test-credential-store', Store())
page.WindowsCredentialStore = lambda root: store
page._credential_requirements = lambda: ({
    'key': 'DASHSCOPE_API_KEY',
    'label': '阿里云百炼 / DashScope',
    'description': '安全保存，不发起请求。',
},)
page._render_credentials(SimpleNamespace(root='unused'), ())
"""
    ).run()

    assert not app.exception
    assert app.text_input[0].label == "安全凭据"
    save = next(button for button in app.button if button.label == "安全保存")
    assert not save.disabled

    app.text_input[0].set_value("unit-test-secret")
    save.click().run()

    assert not app.exception
    rendered = "\n".join(
        str(element.value)
        for collection in (app.markdown, app.caption, app.success)
        for element in collection
    )
    assert "已配置" in rendered
    assert "unit-test-secret" not in rendered
