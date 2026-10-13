"""数据质量与网络工具测试 —— 全部离线运行。

本文件覆盖本项目**最核心的纪律**：防"假成功"。
实测已发生两起翻页失效被当成功的事故，这里把它们固化成回归测试。
"""

import pytest

from tender_notice_cn.common import (
    BLOCKED, NETWORK, STRUCTURE,
    check_paging_integrity, clean_date, decode_html, dedup, is_junk,
    looks_blocked, norm_title, PagingIntegrityError, to_iso_date,
)


# --------------------------------------------------------------- 假翻页检测

def test_real_paging_passes():
    """每页都是新数据 → 不应报警。"""
    check_paging_integrity(raw_count=45, unique_count=45, pages=3)


def test_fake_paging_raises():
    """实测事故：6 页 96 行只有 15 个唯一标题（每条重复 6 次）。"""
    with pytest.raises(PagingIntegrityError) as e:
        check_paging_integrity(raw_count=96, unique_count=15, pages=6)
    assert "重复" in str(e.value)


def test_fake_paging_three_pages():
    """实测事故：48 行只有 14 个唯一标题（每条重复 3 次）。"""
    with pytest.raises(PagingIntegrityError):
        check_paging_integrity(raw_count=48, unique_count=14, pages=3)


def test_empty_input_is_noop():
    check_paging_integrity(0, 0, 0)


def test_small_duplication_does_not_raise():
    """少量跨页边界重复（正常现象）不应报错。"""
    check_paging_integrity(raw_count=100, unique_count=99, pages=5)


# --------------------------------------------------------------- 日期校验

def test_valid_date_kept():
    v, issue = clean_date("2026-10-10", "2026-09-30")
    assert v == "2026-10-10" and issue == ""


def test_deadline_earlier_than_publish_rejected():
    """实测脏值：发布 2026-09-30 / 截止 2025-10-10（早一年）。"""
    v, issue = clean_date("2025-10-10", "2026-09-30")
    assert v == "" and "早于发布" in issue


def test_deadline_two_days_earlier_rejected():
    v, issue = clean_date("2026-09-14", "2026-09-16")
    assert v == "" and "早于发布" in issue


def test_1970_placeholder_rejected():
    v, issue = clean_date("1970-01-01")
    assert v == "" and "占位" in issue


def test_non_date_rejected():
    v, issue = clean_date("详见公告")
    assert v == "" and issue


def test_empty_date():
    assert clean_date("") == ("", "")


def test_date_with_time_is_normalized():
    v, _ = clean_date("2026-10-10 17:00:00", "2026-09-30")
    assert v == "2026-10-10"


def test_to_iso_date_variants():
    assert to_iso_date("2026.10.03 12:04:38") == "2026-10-03"
    assert to_iso_date("2026/10/3") == "2026-10-03"
    assert to_iso_date("2026-10-03") == "2026-10-03"
    assert to_iso_date("没有日期") == ""


# --------------------------------------------------------------- 垃圾行与去重

@pytest.mark.parametrize("text", [
    "招标公告名称", "中标公告名称", "暂无数据", "下一页", "共 1234 条记录", "", "短",
])
def test_junk_detected(text):
    assert is_junk(text)


@pytest.mark.parametrize("text", [
    "某市人民医院医疗设备采购项目公开招标公告",
    "某理工大学省优势特色学科建设设备采购（二）公开招标招标公告",
])
def test_real_titles_not_junk(text):
    assert not is_junk(text)


def test_norm_title_strips_whitespace():
    assert norm_title("  某  项目\u3000公告  ") == "某项目公告"


def test_dedup_keeps_order_and_uniqueness():
    class T:
        def __init__(self, t):
            self.title = t

    items = [T("A 项目公告"), T("B 项目公告"), T("A项目公告"), T("C 项目公告")]
    out = dedup(items)
    assert [x.title for x in out] == ["A 项目公告", "B 项目公告", "C 项目公告"]


# --------------------------------------------------------------- 编码与风控识别

class _Resp:
    def __init__(self, content, ctype="text/html", apparent="utf-8"):
        self.content = content
        self.headers = {"Content-Type": ctype}
        self.apparent_encoding = apparent
        self.encoding = "ISO-8859-1"


def test_decode_uses_meta_charset():
    html = '<meta charset="utf-8"><title>中国政府采购网</title>'.encode("utf-8")
    assert "中国政府采购网" in decode_html(_Resp(html))


def test_decode_falls_back_to_gbk():
    html = '<meta charset="gbk"><title>招标公告</title>'.encode("gbk")
    assert "招标公告" in decode_html(_Resp(html))


def test_decode_does_not_return_mojibake():
    """页面 meta 声明 utf-8 时，默认 ISO-8859-1 解码会得到乱码 —— 必须避免。"""
    html = "<title>频繁访问!中国政府采购网</title>".encode("utf-8")
    text = decode_html(_Resp(html))
    assert "中国政府采购网" in text
    assert "é¢" not in text


@pytest.mark.parametrize("body", [
    "您的访问过于频繁,请稍后再试。",
    "频繁访问!中国政府采购网",
    "很抱歉，由于您访问的URL有可能对网站造成安全威胁，您的访问被阻断。",
])
def test_block_page_detected(body):
    assert looks_blocked(body)


def test_normal_page_not_blocked():
    assert not looks_blocked("<html><title>搜索结果</title>正常内容</html>")


def test_status_constants_are_distinct():
    assert len({BLOCKED, NETWORK, STRUCTURE}) == 3


# --------------------------------------------------- 翻页健康度诊断（drift vs stale）

def test_paging_diagnosis_ok_when_pages_distinct():
    from tender_notice_cn.common import diagnose_paging, PAGING_OK
    pages = [["a", "b", "c"], ["d", "e", "f"]]
    level, msg = diagnose_paging(pages)
    assert level == PAGING_OK and msg == ""


def test_paging_diagnosis_detects_stale():
    """相邻页完全相同 → 翻页未生效（假翻页）。"""
    from tender_notice_cn.common import diagnose_paging, PAGING_STALE
    pages = [["a", "b"], ["a", "b"], ["a", "b"]]
    level, msg = diagnose_paging(pages)
    assert level == PAGING_STALE
    assert "翻页未生效" in msg


def test_paging_diagnosis_detects_drift():
    """页间部分重叠 → 实时增长导致的偏移漂移（**不是**翻页失效）。

    实测：采集"当天"数据时，页3 的前 7 条正好是页1 的第 14~20 条 ——
    因为新公告不断插入头部，整体下移。
    """
    from tender_notice_cn.common import diagnose_paging, PAGING_DRIFT
    page1 = [f"t{i}" for i in range(1, 21)]
    page2 = [f"t{i}" for i in range(14, 34)]
    level, msg = diagnose_paging([page1, page2])
    assert level == PAGING_DRIFT
    assert "实时增长" in msg
    assert "翻页未生效" not in msg          # 不得与假翻页混为一谈


def test_paging_diagnosis_stale_takes_precedence():
    """既有完全相同又有部分重叠时，按更严重的"翻页未生效"报。"""
    from tender_notice_cn.common import diagnose_paging, PAGING_STALE
    pages = [["a", "b"], ["a", "b"], ["b", "c"]]
    level, _ = diagnose_paging(pages)
    assert level == PAGING_STALE


def test_paging_diagnosis_single_page():
    from tender_notice_cn.common import diagnose_paging, PAGING_OK
    assert diagnose_paging([["a", "b"]])[0] == PAGING_OK
    assert diagnose_paging([])[0] == PAGING_OK
