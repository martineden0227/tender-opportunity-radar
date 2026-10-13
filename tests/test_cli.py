"""命令行与"正式版口径"一致性测试 —— 全部离线运行。

本文件里最重要的一组是 `test_official_*`：它们把**正式版（产出 5,702 条的那一版）
的口径锁死**，防止后续改动无意中改变输出条数。
"""

from datetime import date, timedelta

import pytest

from tender_notice_cn import pool
from tender_notice_cn.cli import _parse_delay, _resolve_window, build_parser
from tender_notice_cn.models import (
    PLATFORM_CCGP, RESULT_KEYWORDS, RESULT_KEYWORDS_EXTENDED, Notice,
)


# ----------------------------------------------------------- 正式版口径锁定

# 逐字取自已验收的正式版口径（产出 5,702 条那一版；其原始脚本已归档，
# 口径**以此处为准**）。**改这个列表等于改口径，须先确认。**
OFFICIAL_RESULT_KEYWORDS = (
    "中标公告", "中标结果", "成交结果", "中标(成交)", "候选人公示",
    "中标候选人", "废标", "终止公告", "结果公告", "结果更正",
)


def test_official_result_keywords_unchanged():
    """中标类过滤词表必须与正式版逐字一致（顺序也要一致）。"""
    assert RESULT_KEYWORDS == OFFICIAL_RESULT_KEYWORDS


def test_extended_keywords_is_superset():
    """扩展词表只能在正式版基础上追加，不得删改原有词。"""
    assert set(OFFICIAL_RESULT_KEYWORDS).issubset(set(RESULT_KEYWORDS_EXTENDED))
    for w in OFFICIAL_RESULT_KEYWORDS:
        assert w in RESULT_KEYWORDS_EXTENDED


def test_extended_filter_is_opt_in_by_default():
    """默认必须走正式版口径 —— 不传参时两者结果一致。"""
    items = [Notice(platform=PLATFORM_CCGP, title="某项目流标公告"),
             Notice(platform=PLATFORM_CCGP, title="某项目成交公告"),
             Notice(platform=PLATFORM_CCGP, title="某项目公开招标公告")]
    default_rows, _ = pool.merge(items, retrieve_date="2026-10-13", log=lambda *_: None)
    wider_rows, _ = pool.merge(items, extended_result_filter=True,
                               retrieve_date="2026-10-13", log=lambda *_: None)
    # 正式版口径不认「流标/成交公告」→ 保留；扩展口径剔除
    assert len(default_rows) == 3
    assert len(wider_rows) == 1


def test_extended_filter_catches_more():
    n = Notice(platform=PLATFORM_CCGP, title="某项目流标公告")
    assert not n.is_result_notice
    assert n.is_result_notice_extended


def test_merge_stats_records_filter_scope():
    items = [Notice(platform=PLATFORM_CCGP, title="某项目公开招标公告")]
    _, s1 = pool.merge(items, retrieve_date="2026-10-13", log=lambda *_: None)
    _, s2 = pool.merge(items, extended_result_filter=True,
                       retrieve_date="2026-10-13", log=lambda *_: None)
    assert "正式版" in s1["过滤口径"]
    assert "扩展" in s2["过滤口径"]


# ----------------------------------------------------------- 时间窗：默认当天

def _args(**kw):
    """构造一个最小 args 对象。"""
    base = dict(start="", end="", days=0)
    base.update(kw)
    return type("A", (), base)()


def test_default_window_is_today_only():
    """**默认只采集当天** —— 这是本版的核心变更。"""
    start, end = _resolve_window(_args(days=0))
    assert start == end == date.today().isoformat()


def test_days_positive_widens_window():
    start, end = _resolve_window(_args(days=3))
    assert end == date.today().isoformat()
    assert start == (date.today() - timedelta(days=3)).isoformat()


def test_explicit_start_overrides_days():
    start, end = _resolve_window(_args(days=5, start="2026-09-30", end="2026-10-03"))
    assert (start, end) == ("2026-09-30", "2026-10-03")


def test_start_without_end_ends_today():
    start, end = _resolve_window(_args(start="2026-09-30"))
    assert start == "2026-09-30"
    assert end == date.today().isoformat()


def test_cli_defaults_are_today():
    """两个采集子命令的 --days 默认值都必须是 0（当天）。"""
    p = build_parser()
    for cmd in ("ccgp", "ceb"):
        ns = p.parse_args([cmd])
        assert ns.days == 0, f"{cmd} 的 --days 默认应为 0（当天）"


def test_cli_merge_has_wider_filter_off_by_default():
    p = build_parser()
    ns = p.parse_args(["merge", "a.csv"])
    assert ns.wider_filter is False


def test_parse_delay():
    assert _parse_delay("") is None
    assert _parse_delay("6-10") == (6.0, 10.0)
    assert _parse_delay("8") == (8.0, 8.0)
