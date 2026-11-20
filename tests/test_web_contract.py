"""前端结构契约：导航与锚点的对应规则。

这些断言测的是**规则存在性**，不是渲染像素——内嵌浏览器看不见画面，像素验收归人。
选择这种写法的原因和 `tests/test_field_name_contract.py` 一样：结构约束能被机器
固定，而"看起来对了"不能。
"""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "app" / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "app.js").read_text(encoding="utf-8")


def nav_targets() -> set[str]:
    return set(re.findall(r'class="appnav-link[^"]*"\s+href="#([^"]+)"', HTML))


def nav_ids() -> set[str]:
    """app.js 里滚动同步用的 NAV_IDS。"""
    block = re.search(r"const NAV_IDS = \[([^\]]*)\];", JS)
    assert block, "app.js 里找不到 NAV_IDS，本守卫失去比对对象"
    return set(re.findall(r'"([^"]+)"', block.group(1)))


def js_function(name: str) -> str:
    """取 `function name(...) { … }` 的函数体（浅层花括号匹配）。

    模板串里的 `${…}` 自身成对，因此计数不会错位；真错位时下面的断言会显式报错。
    """
    start = JS.find(f"function {name}(")
    assert start >= 0, f"app.js 里找不到函数：{name}"
    index = JS.index("{", start)
    depth = 0
    for offset in range(index, len(JS)):
        if JS[offset] == "{":
            depth += 1
        elif JS[offset] == "}":
            depth -= 1
            if depth == 0:
                return JS[index + 1 : offset]
    raise AssertionError(f"函数未闭合：{name}")


def test_both_request_builders_send_the_reference_style() -> None:
    """两种模式都要把引用风格发给后端。

    Harness 模式漏传过这个键：后端两层都"看起来支持"，页面选了 APA 仍按 GB/T 排，
    所以守卫钉在"发请求的那两处都带字段"，而不是只测后端。
    """
    for name in ("collectPayload", "submitHarness"):
        body = js_function(name)
        assert "field-reference-style" in body, f"{name} 没有读引用风格选择器"
        assert "reference_style" in body, f"{name} 没有把引用风格发给后端"
    assert JS.count('id = "field-reference-style"') == 1, "选择器 id 必须唯一存在，两处才能读到同一个控件"


def test_nav_links_match_the_scrolled_ids() -> None:
    """顶栏链接与滚动同步的 id 必须是同一套。

    曾经不一致过：`NAV_IDS` 少一个分区 id，那条链接的激活态永不跟随，
    看起来像坏了的按钮。
    """
    assert nav_targets() == nav_ids(), f"顶栏 {sorted(nav_targets())} vs 同步 {sorted(nav_ids())}"


def test_every_nav_anchor_exists_in_the_document() -> None:
    present = set(re.findall(r'id="([^"]+)"', HTML))
    dangling = sorted(nav_targets() - present)
    assert not dangling, f"导航指向文档里不存在的锚点：{dangling}"
