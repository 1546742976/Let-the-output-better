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
