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
    for label in ("LLM", "图片生成", "视频生成", "视觉理解", "语音"):
        assert label in rendered
    assert "MAINLAND_CHINA" in rendered
    assert "已配置" in rendered
    assert "未验证" in rendered
    assert "只影响新 RuntimePlan" in rendered
    assert "secret-value" not in rendered
    assert "API Key" not in rendered
