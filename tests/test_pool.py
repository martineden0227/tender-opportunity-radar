"""候选池合并与导出测试 —— 全部离线运行。"""

import csv
from pathlib import Path

import pytest

from tender_notice_cn import pool
from tender_notice_cn.models import (
    PLATFORM_CCGP, PLATFORM_CEB, POOL_COLUMNS, Notice,
)


def _n(**kw) -> Notice:
    """构造一条 Notice，未指定的字段取默认值。"""
    base = dict(platform=PLATFORM_CCGP, title="某项目公开招标公告",
                url="http://example.invalid/1", publish_date="2026-10-03")
    base.update(kw)
    return Notice(**base)


def test_pool_has_exactly_13_columns():
    """字段规格：上限 13 列，只减不增。"""
    assert len(POOL_COLUMNS) == 13


def test_row_matches_columns():
    row = _n(purchaser="某局", agency="某代理").to_row(1, "2026-10-03")
    assert set(row) == set(POOL_COLUMNS)


def test_who_merged_into_one_column():
    row = _n(purchaser="某局", agency="某代理").to_row(1, "2026-10-03")
    assert row["采购人／代理机构"] == "某局 / 某代理"


def test_missing_fields_are_explicit_not_guessed():
    """列表页没有的字段要写明"需点入详情确认"，不得编造。"""
    row = _n().to_row(1, "2026-10-03")
    assert "需点入详情确认" in row["标段／招标内容"]
    assert "需点入详情确认" in row["预算·限价"]
    assert "需点入详情确认" in row["采购人／代理机构"]


def test_deadline_fallback_is_schedule_not_blank():
    """列表页无截止时间时，应写"以平台日程安排为准"——这是字段级说明，不是缺件。"""
    row = _n().to_row(1, "2026-10-03")
    assert row["投标截止"] == "以平台日程安排为准"


def test_default_verdict_is_pending():
    row = _n().to_row(1, "2026-10-03")
    assert row["总判定"] == "待确认"


def test_result_notice_flagging():
    """默认（正式版口径）的识别边界。

    ⚠️ 注意「成交公告」在正式版词表中**不被识别** ——
    正式版只有「成交结果」。这是刻意保持的口径一致性，
    改动它会使输出条数偏离既有产出。更严的口径见 `is_result_notice_extended`。
    """
    assert _n(title="某项目中标结果公示").is_result_notice
    assert _n(title="某项目中标公告").is_result_notice
    assert _n(title="某项目废标公告").is_result_notice
    assert not _n(title="某项目公开招标公告").is_result_notice

    # 正式版词表不含「成交公告」与「流标」
    assert not _n(title="某项目成交公告").is_result_notice
    assert not _n(title="某项目流标公告").is_result_notice


def test_merge_filters_result_notices():
    """默认口径：只剔除正式版词表命中的类型。"""
    items = [
        _n(title="某项目公开招标公告"),
        _n(title="某项目中标结果公示"),
        _n(title="某项目成交公告"),          # 正式版词表不覆盖 → 保留
    ]
    rows, stats = pool.merge(items, retrieve_date="2026-10-03", log=lambda *_: None)
    assert len(rows) == 2
    assert stats["过滤中标类"] == 1


def test_merge_wider_filter_drops_more():
    """开启扩展口径后，「成交公告」也会被剔除。"""
    items = [
        _n(title="某项目公开招标公告"),
        _n(title="某项目中标结果公示"),
        _n(title="某项目成交公告"),
    ]
    rows, stats = pool.merge(items, extended_result_filter=True,
                             retrieve_date="2026-10-03", log=lambda *_: None)
    assert len(rows) == 1
    assert stats["过滤中标类"] == 2


def test_merge_can_keep_results():
    items = [_n(title="某项目公开招标公告"), _n(title="某项目中标结果公示")]
    rows, _ = pool.merge(items, drop_result_notices=False,
                         retrieve_date="2026-10-03", log=lambda *_: None)
    assert len(rows) == 2


def test_merge_dedups_by_normalized_title():
    items = [
        _n(title="某市人民医院医疗设备采购项目公开招标公告"),
        _n(title="某市人民医院医疗设备采购项目 公开招标公告"),
    ]
    rows, stats = pool.merge(items, retrieve_date="2026-10-03", log=lambda *_: None)
    assert len(rows) == 1 and stats["去重丢弃"] == 1


def test_merge_seq_numbering_continuous():
    items = [_n(title=f"某项目{i}公开招标公告") for i in range(5)]
    rows, _ = pool.merge(items, retrieve_date="2026-10-03", log=lambda *_: None)
    assert [r["序号"] for r in rows] == [1, 2, 3, 4, 5]


def test_merge_across_both_platforms():
    items = [
        _n(platform=PLATFORM_CCGP, title="政府采购某项目公开招标公告"),
        _n(platform=PLATFORM_CEB, title="工程类某项目招标公告"),
    ]
    rows, stats = pool.merge(items, retrieve_date="2026-10-03", log=lambda *_: None)
    assert stats["来源构成"].keys() == {"全国级·中国政府采购网",
                                        "全国级·中国招标投标公共服务平台"}
    assert all("全国级·" in r["来源平台"] for r in rows)


def test_write_csv_roundtrip(tmp_path: Path):
    rows, _ = pool.merge([_n(title="某项目公开招标公告")],
                         retrieve_date="2026-10-03", log=lambda *_: None)
    out = tmp_path / "pool.csv"
    pool.write_csv(rows, str(out), log=lambda *_: None)

    with open(out, encoding="utf-8-sig") as f:
        read = list(csv.DictReader(f))
    assert len(read) == 1
    assert list(read[0].keys()) == POOL_COLUMNS


def test_write_markdown_states_scope_boundary(tmp_path: Path):
    """Markdown 版必须写明口径边界与"待确认"，避免被当成合格清单用。"""
    rows, stats = pool.merge([_n(title="某项目公开招标公告")],
                             retrieve_date="2026-10-03", log=lambda *_: None)
    out = tmp_path / "pool.md"
    pool.write_markdown(rows, str(out), window="2026-09-30 ~ 2026-10-03",
                        stats=stats, log=lambda *_: None)
    text = out.read_text(encoding="utf-8")
    assert "待确认" in text
    assert "口径边界" in text or "全量" in text
