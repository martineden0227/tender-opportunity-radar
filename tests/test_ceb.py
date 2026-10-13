"""平台二（中国招标投标公共服务平台）解析测试 —— 全部离线运行。"""

import json
from pathlib import Path

import pytest

from tender_notice_cn import ceb
from tender_notice_cn.models import PLATFORM_CEB

FIXTURES = Path(__file__).resolve().parents[1] / "src" / "tender_notice_cn" / "fixtures"


@pytest.fixture(scope="module")
def payload() -> dict:
    return json.loads((FIXTURES / "ceb_response.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def items(payload):
    return ceb.parse_records((payload.get("object") or {}).get("returnlist") or [])


def test_parses_all_valid_rows(items):
    """样例 5 条中有 1 条标题为空，应产出 4 条。"""
    assert len(items) == 4


def test_platform_tag(items):
    assert all(n.platform == PLATFORM_CEB for n in items)


def test_core_fields(items):
    for n in items:
        assert n.title
        assert n.area
        assert n.industry
        assert n.publish_date
        assert n.platform_name


def test_1970_placeholder_is_cleared(items):
    """bulletinEndTime 的 1970-01-01 是空值占位，必须置空并留下标注。"""
    flagged = [n for n in items if "1970-01-01" in n.issues]
    assert len(flagged) == 1
    assert flagged[0].deadline == ""


def test_valid_deadline_kept(items):
    with_dl = [n for n in items if n.deadline]
    assert with_dl, "应至少有一条有效结束时间"
    assert all(n.deadline >= n.publish_date for n in with_dl)


def test_detail_url_built_from_business_id(items):
    n = items[0]
    assert n.url.endswith(n.extra.get("businessId", "") or n.url.split("=")[-1])
    assert "searchBulletinDetail" in n.url


def test_payload_shape():
    p = ceb.build_payload(keyword="电梯", row=500)
    assert p["pageNo"] == "1"
    assert p["row"] == "500"
    assert p["businessType"] == "招标公告"
    assert p["searchName"] == "电梯"


def test_row_default_is_measured_max():
    """row 是突破单页容量的关键参数，默认值应等于实测上限。"""
    assert ceb.MAX_ROW == 500
    assert ceb.build_payload()["row"] == str(ceb.MAX_ROW)


def test_default_keywords_cover_three_layers():
    """词表须覆盖工程／货物／服务三层，缺一层就漏一批项目。"""
    kw = ceb.DEFAULT_KEYWORDS
    assert len(kw) >= 40
    for token in ("房屋建筑", "设备采购", "咨询服务"):
        assert token in kw


def test_empty_records():
    assert ceb.parse_records([]) == []
    assert ceb.parse_records(None) == []


def test_cross_slice_duplicates_are_not_treated_as_paging_failure():
    """跨关键词分片出现重复是设计使然（同一公告命中多个关键词），
    不得按"翻页失效"处理。

    这是实测踩过的坑：早期实现把分片去重当成翻页完整性校验，
    跑 50 个词时报出「1786 行只有 256 条唯一」的假告警，误导排查方向。
    """
    from tender_notice_cn.common import check_paging_integrity, PagingIntegrityError

    # ceb 适配器**不调用**该判据；此处固化"若按分片口径调用会被误报"这一事实，
    # 提醒后来者不要把两者混用。
    with pytest.raises(PagingIntegrityError):
        check_paging_integrity(raw_count=1786, unique_count=256, pages=1)

    # 而按"页码翻页"口径，少量重复是允许的
    check_paging_integrity(raw_count=100, unique_count=97, pages=5)
