"""平台一（中国政府采购网）解析测试 —— 全部离线运行。"""

import json
from pathlib import Path

import pytest

from tender_notice_cn import ccgp
from tender_notice_cn.models import PLATFORM_CCGP

FIXTURES = Path(__file__).resolve().parents[1] / "src" / "tender_notice_cn" / "fixtures"


@pytest.fixture(scope="module")
def html() -> str:
    return (FIXTURES / "ccgp_search.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def items(html):
    return ccgp.parse_search_result(html)


def test_parses_all_valid_rows(items):
    """样例页含 9 条有效数据 + 1 条表头垃圾行 → 应解析出 9 条。"""
    assert len(items) == 9


def test_junk_header_row_filtered(items):
    """表头行「招标公告名称」不得进入结果。"""
    assert all("招标公告名称" not in n.title for n in items)


def test_platform_tag(items):
    assert all(n.platform == PLATFORM_CCGP for n in items)


def test_core_fields_complete(items):
    for n in items:
        assert n.title
        assert n.url.startswith("http")
        assert n.publish_date, f"缺发布时间：{n.title}"
        assert n.purchaser, f"缺采购人：{n.title}"
        assert n.agency, f"缺代理机构：{n.title}"
        assert n.category, f"缺公告类型：{n.title}"


def test_publish_date_normalized_from_dotted_format(items):
    """真实页面日期写作 2026.10.03 12:04:38，须归一化为 2026-10-03。"""
    assert items[0].publish_time.startswith("2026.10.03")
    assert items[0].publish_date == "2026-10-03"


def test_area_is_region_not_notice_category(items):
    """回归测试：地区字段不得装成"公告类型"。

    误按分隔段位置取数时，地区列会拿到「公开招标公告」这类值 —— 且不会报错。
    """
    for n in items:
        assert "公告" not in n.area, f"地区字段被污染：{n.area!r} ({n.title[:20]})"
        assert n.area != n.category


def test_area_extracted_when_present(items):
    areas = {n.title[:12]: n.area for n in items}
    assert any(v == "山东" for v in areas.values())
    assert any(v == "浙江" for v in areas.values())
    assert any(v == "湖北" for v in areas.values())


def test_area_may_be_empty_and_is_not_guessed(items):
    """地区在真实页面中经常整体缺失 —— 缺失时应留空，不得用类型填补。"""
    assert any(n.area == "" for n in items)


def test_agency_not_polluted_by_category(items):
    """代理机构尾部不得粘上公告类型（真实页面 <br/> 不产生分隔符）。"""
    for n in items:
        assert not n.agency.endswith(n.category or "\0")
        assert "公告" not in n.agency, f"代理机构被污染：{n.agency!r}"


def test_category_values_are_notice_types(items):
    from collections import Counter
    cats = Counter(n.category for n in items)
    assert "公开招标公告" in cats
    assert "竞争性磋商公告" in cats
    assert "更正公告" in cats


def test_result_notice_detection(items):
    """中标/成交/结果类应被识别，普通更正类不应被误伤。

    注意：标题含「采购结果更正」的属结果类公告（是对结果公告的更正），
    应被识别；而标题只含「更正公告」的属机会类公告的变更，不应被过滤。
    """
    flagged = [n for n in items if n.is_result_notice]
    assert any("结果更正" in n.title for n in flagged)

    plain_corrections = [n for n in items
                         if "更正公告" in n.title and "结果" not in n.title]
    assert plain_corrections, "样例应包含普通更正公告"
    assert not any(n.is_result_notice for n in plain_corrections)


def test_tender_notices_are_kept(items):
    """公开招标/竞争性磋商类属机会级，不得被结果过滤误伤。"""
    tender = [n for n in items if "招标公告" in n.category or "磋商" in n.category]
    assert tender and not any(n.is_result_notice for n in tender)


def test_build_params_date_filter_is_time_type_6():
    """日期筛选依赖 timeType=6，缺了它服务端会忽略起止日期。"""
    p = ccgp.build_params(2, start_time="2026:10:01", end_time="2026:10:03")
    assert p["timeType"] == "6"
    assert p["start_time"] == "2026:10:01"
    assert p["page_index"] == "2"
    assert p["dbselect"] == "bidx"


def test_params_are_url_encodable():
    from urllib.parse import urlencode
    s = urlencode(ccgp.build_params(1, keyword="电梯"))
    assert "%E7%94%B5%E6%A2%AF" in s


def test_empty_html_returns_empty_list():
    assert ccgp.parse_search_result("<html><body>没有结果</body></html>") == []


# ---------------------------------------- 采购品目 / 地区 的字段归属（2026-10-13 实测踩坑）

def test_second_strong_is_purchase_category(items):
    """第 2 个 <strong> 是采购品目，应存入 industry，不得进 area。"""
    assert any(n.industry for n in items), "样例应含采购品目"
    for n in items:
        assert n.industry != n.area


def test_short_purchase_category_not_mistaken_for_area(items):
    """回归测试：短品目骗过启发式过滤。

    真实页面里「服务/其他服务」只有 7 个字，若按
    「取最后一个分隔段 + 长度 ≤ 10 则视为地区」来取数，它会被写进「区域」列，
    而且**不会报错** —— 实测当天 246 条里污染了 16 条。
    必须按 DOM 位置（两个 <strong> 之间的纯文本）判定。
    """
    n = next(x for x in items if "数据要素大赛" in x.title)
    assert n.area == "山东", f"地区取错：{n.area!r}"
    assert n.industry == "服务/其他服务", f"品目取错：{n.industry!r}"


def test_area_never_contains_slash_or_notice_word(items):
    """地区字段的硬约束：不得含 '/'（品目特征）或 '公告'（类型特征）。"""
    for n in items:
        assert "/" not in n.area, f"地区被写成品目：{n.area!r} ({n.title[:20]})"
        assert "公告" not in n.area, f"地区被写成类型：{n.area!r}"
