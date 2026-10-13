"""命令行入口。

    tender-notice check  ccgp                 # 连通性自检
    tender-notice ccgp   --days 3 --pages 5   # 采集平台一
    tender-notice ceb    --days 3             # 采集平台二
    tender-notice merge  a.csv b.csv          # 合并为候选池
    tender-notice demo                        # 离线演练（不联网，验证解析）

设计取向：**默认保守**。间隔不低、页数不贪、遇错即停并说清原因 ——
宁可少给数据，也不拿重复数据充数。
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, timedelta
from pathlib import Path

from . import ceb, ccgp, pool
from .common import today_iso
from .models import Notice, PLATFORM_CCGP, PLATFORM_CEB

FIXTURES = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------- 工具

def _parse_delay(text: str) -> tuple[float, float] | None:
    if not text:
        return None
    a, _, b = text.partition("-")
    lo = float(a)
    return (lo, float(b or a))


def _resolve_window(args) -> tuple[str, str]:
    """把 --days / --start / --end 解析成 (start, end)，均为 YYYY-MM-DD。"""
    if getattr(args, "start", ""):
        return args.start, (args.end or date.today().isoformat())
    days = getattr(args, "days", 3) or 3
    end = date.today()
    return (end - timedelta(days=days)).isoformat(), end.isoformat()


def _write_simple_csv(notices: list[Notice], path: str, log=print) -> str:
    cols = ["platform", "title", "url", "publish_time", "publish_date",
            "purchaser", "agency", "area", "category", "industry",
            "deadline", "project_code", "platform_name", "issues"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for n in notices:
            d = n.as_dict()
            w.writerow({c: d.get(c, "") for c in cols})
    log(f"\n已写入 {len(notices)} 条 → {path}")
    return path


def _load_csv(paths: list[str]) -> list[Notice]:
    """读回采集阶段的 CSV，转成 Notice（供 merge 使用）。"""
    out: list[Notice] = []
    fields = set(Notice.__dataclass_fields__) - {"extra"}
    for p in paths:
        with open(p, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                out.append(Notice(**{k: v for k, v in row.items() if k in fields}))
    return out


# --------------------------------------------------------------------- 子命令

def cmd_check(args) -> int:
    log = print
    targets = ["ccgp", "ceb"] if args.platform == "all" else [args.platform]
    results = {}
    for t in targets:
        log("=" * 62)
        log(f"自检：{t}")
        log("=" * 62)
        if t == "ccgp":
            results[t] = ccgp.check(use_proxy=args.use_proxy, days=args.days, log=log)
        else:
            results[t] = ceb.check(use_proxy=args.use_proxy, log=log)
        log("")
    ok = all(results.values())
    log(f"结论：{sum(results.values())}/{len(results)} 个平台可用")
    return 0 if ok else 1


def cmd_ccgp(args) -> int:
    start, end = _resolve_window(args)
    print(f"平台：中国政府采购网　区间：{start} ~ {end}")
    print("（该平台日期筛选在服务端真实生效，取数严格落在区间内）\n")
    items = ccgp.collect(
        pages=args.pages, keyword=args.keyword, pinmu=args.pinmu,
        bid_type=args.bidtype,
        start_time=start.replace("-", ":"), end_time=end.replace("-", ":"),
        use_proxy=args.use_proxy, delay=_parse_delay(args.delay),
    )
    if not items:
        print("\n未取到数据。建议先跑：tender-notice check ccgp")
        return 1
    _write_simple_csv(items, args.output)
    return 0


def cmd_ceb(args) -> int:
    start, stop = _resolve_window(args)
    print(f"平台：中国招标投标公共服务平台　时间窗：{start} ~ {stop}")
    print("（该平台日期筛选在服务端**无效**，时间窗在本地按接收时间筛选）\n")
    words = None
    if args.keywords:
        words = [w.strip() for w in args.keywords.split(",") if w.strip()]
    items = ceb.collect(
        start=start, stop=stop, keywords=words, industry=args.industry,
        row=args.row, delay=args.delay, use_proxy=args.use_proxy,
    )
    if not items:
        print("\n未取到数据。建议先跑：tender-notice check ceb")
        return 1
    _write_simple_csv(items, args.output)
    return 0


def cmd_merge(args) -> int:
    notices = _load_csv(args.inputs)
    print(f"读入 {len(notices)} 条（{len(args.inputs)} 个文件）")
    rows, stats = pool.merge(notices, drop_result_notices=not args.keep_results,
                             retrieve_date=args.retrieve_date or today_iso())
    pool.write_csv(rows, args.output)
    if args.markdown:
        pool.write_markdown(rows, args.markdown,
                            window=args.window or "", stats=stats)
    return 0


def cmd_demo(args) -> int:
    """离线演练：用内置样例验证解析链路，不联网。"""
    print("=" * 62)
    print("离线演练（不联网）")
    print("=" * 62)

    ccgp_html = (FIXTURES / "ccgp_search.html").read_text(encoding="utf-8")
    a = ccgp.parse_search_result(ccgp_html)
    print(f"\n[平台一] 解析样例页 → {len(a)} 条")
    for n in a[:3]:
        print(f"   {n.title[:40]}")
        print(f"      时间={n.publish_date} | 采购人={n.purchaser[:16]} | "
              f"代理={n.agency[:18]}")
        print(f"      类型={n.category} | 地区={n.area or '(空)'}")

    import json
    ceb_json = json.loads((FIXTURES / "ceb_response.json").read_text(encoding="utf-8"))
    b = ceb.parse_records((ceb_json.get("object") or {}).get("returnlist") or [])
    print(f"\n[平台二] 解析样例响应 → {len(b)} 条")
    for n in b[:3]:
        print(f"   {n.title[:40]}")
        print(f"      时间={n.publish_date} | 区域={n.area} | 专业={n.industry}")
        print(f"      截止={n.deadline or '(空)'} | 项目代码={n.project_code}")

    rows, _ = pool.merge(a + b, retrieve_date=today_iso(), log=lambda *_: None)
    if args.output:
        pool.write_csv(rows, args.output)
    print(f"\n[合并] 候选池 {len(rows)} 条"
          f"{'（已写入 ' + args.output + '）' if args.output else ''}")
    print("\n离线演练通过。")
    return 0


# --------------------------------------------------------------------- 装配

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tender-notice",
        description="中国招标公告采集工具（中国政府采购网 / 中国招标投标公共服务平台）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--version", action="version", version="tender-notice-cn 0.1.0")
    sub = p.add_subparsers(dest="cmd", required=True)

    # check
    c = sub.add_parser("check", help="连通性自检（不进入长退避）")
    c.add_argument("platform", choices=["ccgp", "ceb", "all"], nargs="?", default="all")
    c.add_argument("--days", type=int, default=3)
    c.add_argument("--use-proxy", action="store_true",
                   help="走系统代理（默认直连；境内站点通常应直连）")
    c.set_defaults(func=cmd_check)

    # ccgp
    a1 = sub.add_parser("ccgp", help="采集中国政府采购网")
    a1.add_argument("--pages", type=int, default=3)
    a1.add_argument("--keyword", default="")
    a1.add_argument("--pinmu", default="0", help="0不限 1货物 2工程 3服务")
    a1.add_argument("--bidtype", default="0", help="公告类型，0 为不限")
    a1.add_argument("--days", type=int, default=3)
    a1.add_argument("--start", default="", help="YYYY-MM-DD")
    a1.add_argument("--end", default="", help="YYYY-MM-DD")
    a1.add_argument("--delay", default="", help="请求间隔秒，如 8-14（默认 6-10）")
    a1.add_argument("--use-proxy", action="store_true")
    a1.add_argument("--output", default="ccgp_notices.csv")
    a1.set_defaults(func=cmd_ccgp)

    # ceb
    a2 = sub.add_parser("ceb", help="采集中国招标投标公共服务平台")
    a2.add_argument("--days", type=int, default=3)
    a2.add_argument("--start", default="", help="YYYY-MM-DD")
    a2.add_argument("--end", default="", help="YYYY-MM-DD")
    a2.add_argument("--row", type=int, default=ceb.MAX_ROW,
                    help=f"单次返回条数，实测上限 {ceb.MAX_ROW}")
    a2.add_argument("--keywords", default="", help="自定义分片关键词，逗号分隔")
    a2.add_argument("--industry", default="", help="行业分类（该参数服务端生效）")
    a2.add_argument("--delay", type=float, default=ceb.DEFAULT_DELAY)
    a2.add_argument("--use-proxy", action="store_true")
    a2.add_argument("--output", default="ceb_notices.csv")
    a2.set_defaults(func=cmd_ceb)

    # merge
    a3 = sub.add_parser("merge", help="把采集结果合并为候选池")
    a3.add_argument("inputs", nargs="+", help="采集阶段输出的 CSV")
    a3.add_argument("--output", default="project_pool.csv")
    a3.add_argument("--markdown", default="", help="同时输出 Markdown 版")
    a3.add_argument("--window", default="", help="时间窗描述，写进 Markdown")
    a3.add_argument("--retrieve-date", default="")
    a3.add_argument("--keep-results", action="store_true",
                    help="保留中标/成交类公告（默认过滤）")
    a3.set_defaults(func=cmd_merge)

    # demo
    a4 = sub.add_parser("demo", help="离线演练（不联网）")
    a4.add_argument("--output", default="", help="可选：把合并结果写入 CSV")
    a4.set_defaults(func=cmd_demo)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
