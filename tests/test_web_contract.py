"""前端结构契约：导航、锚点与对照面板的常驻规则。

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
CSS = (WEB / "styles.css").read_text(encoding="utf-8")


def nav_targets() -> set[str]:
    return set(re.findall(r'class="appnav-link[^"]*"\s+href="#([^"]+)"', HTML))


def nav_ids() -> set[str]:
    """app.js 里滚动同步用的 NAV_IDS。"""
    block = re.search(r"const NAV_IDS = \[([^\]]*)\];", JS)
    assert block, "app.js 里找不到 NAV_IDS，本守卫失去比对对象"
    return set(re.findall(r'"([^"]+)"', block.group(1)))


def rule_body(selector: str, text: str) -> str:
    """取形如 `selector { ... }` 的规则体（浅层括号匹配，够用且失败会显式报错）。"""
    start = text.find(f"{selector} {{")
    assert start >= 0, f"styles.css 缺少规则：{selector}"
    depth = 0
    index = text.index("{", start)
    for offset in range(index, len(text)):
        if text[offset] == "{":
            depth += 1
        elif text[offset] == "}":
            depth -= 1
            if depth == 0:
                return text[index + 1 : offset]
    raise AssertionError(f"规则未闭合：{selector}")


def media_block(max_width: str) -> str:
    head = "@media (max-width: " + max_width + ") {"
    start = CSS.find(head)
    assert start >= 0, f"styles.css 缺少媒体块：{head}"
    depth = 0
    for offset in range(start + len(head) - 1, len(CSS)):
        if CSS[offset] == "{":
            depth += 1
        elif CSS[offset] == "}":
            depth -= 1
            if depth == 0:
                return CSS[start + len(head) - 1 : offset + 1]
    raise AssertionError(f"媒体块未闭合：{head}")


def test_nav_links_match_the_scrolled_ids() -> None:
    """顶栏链接与滚动同步的 id 必须是同一套。

    曾经不一致过：`NAV_IDS` 少一个 `section-diff`，那条链接的激活态永不跟随，
    看起来像坏了的按钮——这正是本次被要求去掉的东西。
    """
    assert nav_targets() == nav_ids(), f"顶栏 {sorted(nav_targets())} vs 同步 {sorted(nav_ids())}"


def test_every_nav_anchor_exists_in_the_document() -> None:
    present = set(re.findall(r'id="([^"]+)"', HTML))
    dangling = sorted(nav_targets() - present)
    assert not dangling, f"导航指向文档里不存在的锚点：{dangling}"


def test_diff_is_not_a_nav_entry_but_still_a_panel() -> None:
    """删的是入口，不是内容：链接不该在，面板必须还在。

    后半句专门抓"把对照一起删掉"这种错法。
    """
    assert 'href="#section-diff"' not in HTML, "顶栏仍留着对照锚点"
    assert 'id="section-diff"' in HTML, "对照面板被误删：常态化不等于消失"


def test_wide_screen_keeps_the_diff_visible() -> None:
    env = rule_body("#section-env", CSS)
    diff = rule_body("#section-diff", CSS)
    assert "max-height" in env, "环境面板没有高度上限，长路径会挤掉对照"
    assert "min-height" in diff, "对照没有最小可见高度"


def test_narrow_screen_docks_the_diff_at_the_bottom() -> None:
    block = media_block("1040px")
    body = rule_body("#section-diff", block)
    assert "position: sticky" in body, "窄屏下对照没有粘性锚定，要滚到整篇末尾才看到"
    assert "bottom:" in body, "sticky 缺少 bottom，不会锚在视口底部"
