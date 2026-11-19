"""配置告警：把"写进 .env 就以为生效"的组合变成可读的一句话。

这些都不是崩溃错误，所以只能靠告警通道暴露；反过来，正常配置必须得到空列表，
否则这个通道会退化成永远非空的噪声，很快没人看。
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient

import app.main as api
from app.settings import PipelineConfig, Settings, collect_config_warnings


def make(**kwargs) -> Settings:
    base = {"_env_file": None, "provider": "mock", "audit_path": "unused-in-tests"}
    base.update(kwargs)
    return Settings(**base)


def test_disabled_harness_stays_silent():
    assert collect_config_warnings(make(harness_enabled=False, harness_providers="codx")) == []


def test_clean_config_produces_no_warnings():
    assert (
        collect_config_warnings(make(harness_command=sys.executable, harness_allowed_commands=[sys.executable])) == []
    )


def test_unknown_provider_name_is_reported_with_the_valid_ones():
    warnings = collect_config_warnings(make(harness_providers="codx,openai-api"))
    assert len(warnings) == 1
    assert "codx" in warnings[0]
    assert "codex" in warnings[0], "告警要给出可选名字，否则还得再去翻代码"


def test_unknown_profile_is_reported():
    warnings = collect_config_warnings(make(harness_profile="claude"))
    assert any("HARNESS_PROFILE" in text for text in warnings)


def test_args_without_command_are_dead_configuration():
    warnings = collect_config_warnings(
        make(harness_command="", harness_args=["a.js"], harness_allowed_commands=["node"])
    )
    assert any("HARNESS_ARGS" in text and "不生效" in text for text in warnings)


def test_credential_shaped_env_allow_is_flagged():
    warnings = collect_config_warnings(make(harness_env_allow="PATH,OPENAI_API_KEY"))
    assert any("凭据" in text for text in warnings)


@contextmanager
def served(settings: Settings) -> Iterator[Callable[[], TestClient]]:
    """用给定配置起 TestClient，不触碰真实 .env，也不碰本机任何 harness。"""
    previous: tuple[Callable, Callable] = (api.get_settings, api.get_pipeline_config)
    api.get_settings = lambda: settings
    api.get_pipeline_config = lambda: PipelineConfig(audit=False)
    try:
        with TestClient(api.app) as client:
            yield lambda: client
    finally:
        api.get_settings, api.get_pipeline_config = previous


def test_every_warning_kind_reaches_the_page():
    for bad in ("codx", "not-a-profile"):
        with served(make(harness_providers=bad)) as client:
            warnings = client().get("/v1/config").json()["warnings"]
        assert any(bad in text for text in warnings), f"{bad} 拼错却毫无提示"


def test_warnings_key_is_absent_of_noise_in_a_normal_setup():
    with served(make(harness_command=sys.executable, harness_allowed_commands=[sys.executable])) as client:
        assert client().get("/v1/config").json()["warnings"] == []
