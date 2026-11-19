"""请求体字段名单的一致性：页面的开关名必须就是服务端认的字段名。

这条守卫来自一个真实故障——页面把 `score` 写成 `rules`，在 `extra="ignore"` 下
被静默丢弃：勾选框能点、请求 200、评分照跑，没有任何地方报错。`forbid` 让拼错
当场可见，本文件则让"页面名单与服务端名单不一致"在合入前就红。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import Options, app

WEB = Path(__file__).resolve().parents[1] / "app" / "web"


def option_keys_from_page() -> set[str]:
    source = (WEB / "app.js").read_text(encoding="utf-8")
    block = re.search(r"const OPTION_FIELDS = \[(.*?)\];", source, re.S)
    assert block, "页面里找不到 OPTION_FIELDS，本守卫失去比对对象"
    return set(re.findall(r'\{ key: "([a-z_]+)"', block.group(1)))


def step_option_values_from_page() -> set[str]:
    source = (WEB / "app.js").read_text(encoding="utf-8")
    block = re.search(r"const STEP_OPTION = \{([^}]*)\};", source)
    assert block, "页面里找不到 STEP_OPTION，本守卫失去比对对象"
    return set(re.findall(r'"([a-z_]+)"', block.group(1)))


def server_option_keys() -> set[str]:
    return set(Options.model_fields) - {"timeout_ms"}


def test_page_toggles_match_server_options_fields():
    assert option_keys_from_page() == server_option_keys()


def test_page_step_mapping_only_references_real_options():
    unknown = step_option_values_from_page() - server_option_keys()
    assert not unknown, f"STEP_OPTION 指向服务端没有的开关：{sorted(unknown)}"


@pytest.mark.parametrize("method,path", [("POST", "/v1/optimize"), ("POST", "/v1/run")])
def test_unknown_option_key_is_rejected_not_swallowed(method: str, path: str):
    client = TestClient(app)
    body = {"raw_output": "hi"} if path == "/v1/optimize" else {"task": "hi"}
    body["options"] = {"rules": False}
    response = client.request(method, path, json=body)
    assert response.status_code == 422
    assert "rules" in json.dumps(response.json(), ensure_ascii=False)


def test_score_toggle_actually_disables_the_rules_step():
    """勾选"评分"必须真的落到 rules 步骤上——这正是此前失效的那条链路。"""
    client = TestClient(app)
    off = client.post(
        "/v1/optimize",
        json={"raw_output": "值得注意的是，本文方法具有广阔的应用前景。", "options": {"score": False}},
    )
    on = client.post(
        "/v1/optimize",
        json={"raw_output": "值得注意的是，本文方法具有广阔的应用前景。", "options": {"score": True}},
    )
    assert off.status_code == 200 and on.status_code == 200
    assert "rules" not in [step["name"] for step in off.json()["steps"]]
    assert "rules" in [step["name"] for step in on.json()["steps"]]
