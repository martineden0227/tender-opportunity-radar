"""合并为「项目候选池」。

把两个平台的采集结果合并成一张 13 列的候选池表，并做两件事：

1. **按归一化标题全局去重** —— 同一项目可能在两个平台都发布，标题写法不同，
   本函数按去掉空白后的标题前 50 字比对，仍可能存在同一项目的不同表述各占一行，
   交付时要如实说明这一点。
2. **过滤中标类公告** —— 中标／成交／候选人／废标／终止属「研究竞争对手」环节，
   不进「机会级」候选池（归属另一个技能）。

字段严格 13 列。想记的附加信息写进「备注」，**不新增列**。
"""

from __future__ import annotations

import csv
from collections import Counter

from .common import norm_title, today_iso
from .models import POOL_COLUMNS, Notice


def merge(notices: list[Notice], *, drop_result_notices: bool = True,
          extended_result_filter: bool = False,
          retrieve_date: str = "", log=print):
    """合并去重，返回 (行列表, 统计字典)。

    参数
    ----
    drop_result_notices
        是否过滤中标/成交类公告（默认 True）。
    extended_result_filter
        是否使用**更严**的过滤口径。
        默认 False —— 使用与正式版（5,702 条那版）**逐字一致**的词表，
        以保证与既有产出可比。
        置 True 会额外过滤「流标／成交公告／结果公示」等
        （实测多滤 52 条，这些确属非机会类，但会改变条数口径）。
    """
    rdate = retrieve_date or today_iso()

    seen: set[str] = set()
    rows: list[dict] = []
    dropped_dup = dropped_result = 0

    for n in notices:
        if not n.title.strip():
            continue
        if drop_result_notices:
            hit = (n.is_result_notice_extended if extended_result_filter
                   else n.is_result_notice)
            if hit:
                dropped_result += 1
                continue
        k = norm_title(n.title)
        if k in seen:
            dropped_dup += 1
            continue
        seen.add(k)
        rows.append(n.to_row(len(rows) + 1, rdate))

    stats = {
        "输入条数": len(notices),
        "去重丢弃": dropped_dup,
        "过滤中标类": dropped_result,
        "输出条数": len(rows),
        "过滤口径": ("扩展（含流标/成交公告/结果公示）" if extended_result_filter
                     else "正式版核心词表"),
        "来源构成": dict(Counter(r["来源平台"] for r in rows)),
    }
    log(f"合并：输入 {len(notices)} → 去重丢弃 {dropped_dup} → "
        f"过滤中标类 {dropped_result} → 输出 {len(rows)} 条")
    return rows, stats


def write_csv(rows: list[dict], path: str, log=print) -> str:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=POOL_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in POOL_COLUMNS})
    log(f"已写入 {len(rows)} 条 → {path}")
    return path


def write_markdown(rows: list[dict], path: str, *, window: str = "",
                   stats: dict | None = None, preview: int = 80, log=print) -> str:
    """生成便于人看的 Markdown 版候选池（含口径说明）。"""
    lines = [
        "# 项目候选池", "",
        f"**时间窗**：{window or '未指定'}　**记录数**：{len(rows)}", "",
    ]
    if stats:
        lines += ["## 采集口径", ""]
        for k, v in stats.items():
            lines.append(f"- {k}：{v}" if not isinstance(v, dict) else f"- {k}：{v}")
        lines += [
            "",
            "> ⚠️ **口径边界**：本表是「按时段 + 关键词命中的公告」，"
            "若数据来自公共服务平台，因该平台**日期筛选在服务端无效**，"
            "时间窗是本地按「接收时间」筛出的，**不等于该时段全量公告**；"
            "且接收时间含提前发布与预告。", "",
        ]

    lines += ["## 摘要（前 %d 条）" % min(preview, len(rows)), "",
              "| 序号 | 项目名称 | 采购人／代理机构 | 区域 | 来源 |",
              "|---|---|---|---|---|"]
    for r in rows[:preview]:
        lines.append(
            f"| {r['序号']} | {str(r['项目名称'])[:40]} | "
            f"{str(r['采购人／代理机构'])[:24]} | {str(r['区域'])[:8]} | "
            f"{str(r['来源平台'])[-8:]} |"
        )
    if len(rows) > preview:
        lines.append(f"\n（完整 {len(rows)} 条见 CSV）")

    lines += [
        "", "## 使用前必读", "",
        "- 全部记录的总判定为 **待确认** —— 资格核验需要投标方自身的",
        "  主体资格／资质／业绩／信用材料，**未提供前任何一条都不得判为合格**。",
        "- `投标截止` 为「以平台日程安排为准」时，表示列表页未提供该字段，",
        "  需点入详情或到平台「日程安排」栏目查询，**不是数据缺失**。",
    ]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log(f"已写入 Markdown → {path}")
    return path


__all__ = ["merge", "write_csv", "write_markdown"]
