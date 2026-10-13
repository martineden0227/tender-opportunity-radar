"""适配器一：中国政府采购网（政府采购体系）。

实测要点（2026-10 复核，详见 docs/platform-notes.md）：

* 入口是**独立的参数化检索接口**，不是目录页（目录页多为前端 JS 渲染）。
* **日期筛选服务端真实生效**（`timeType=6` + `start_time`/`end_time` 是真参数），
  取数严格落在给定区间内 —— 这一点与平台二正好相反。
* **翻页真实生效**（`page_index` 参数化），无假翻页。
* 列表页即含**采购人、代理机构、公告类型**，详情链接可直接打开。
* 被限流时返回 **HTTP 200** + 约 2.9 KB 的拦截页（**不是 403**）；
  限流是短时的，等待约 60 秒即恢复，间隔 6~10 秒可长时间稳定采集。

⚠️ 字段结构陷阱：常见误写的结构注释是
「时间 | 采购人 | 代理机构 编号 | 地区 | 类别」，**与真实页面不符** ——
实际没有"编号"字段，第 4 段是**公告类型**，第 5 段才是地区（且经常为空）。
按分隔段位置取数会产出**错位字段**（地区列里装"公开招标公告"）且不报错。
本模块按标签与中文标记定位，不依赖位置序号。
"""

from __future__ import annotations

import random
import re
import time
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

from .common import (
    BLOCKED, DEFAULT_TIMEOUT, DEFAULT_UA, NETWORK, STRUCTURE,
    build_session, check_paging_integrity, clean_date, decode_html,
    is_junk, looks_blocked, norm_title, retry_call, to_iso_date,
)
from .models import PLATFORM_CCGP, Notice

SEARCH_URL = "http://search.ccgp.gov.cn/bxsearch"
REFERER = "http://www.ccgp.gov.cn/"

RESULT_SELECTOR = ".vT-srch-result-list-bid"

# 请求间隔（秒）。官方限流是分钟级的，间隔够大即可长时间稳定。
DEFAULT_DELAY = (6.0, 10.0)
BLOCK_WAIT = 90.0
MAX_BLOCK_RETRY = 3
MAX_NET_RETRY = 4


def _headers() -> dict:
    return {"Referer": REFERER, "User-Agent": DEFAULT_UA}


def build_params(page_index: int, keyword: str = "", pinmu: str = "0",
                 bid_type: str = "0", start_time: str = "", end_time: str = "") -> dict:
    """构造检索参数。

    | 参数 | 作用 | 实测 |
    |---|---|---|
    | `searchtype=1` | 固定值 | — |
    | `page_index` | 页码，从 1 起 | ✅ 翻页真实生效 |
    | `dbselect=bidx` | 检索招标公告 | — |
    | `pinMu` | 品目：0 不限／1 货物／2 工程／3 服务 | 按需使用 |
    | `bidType` | 公告类型，0 为不限 | 按需使用 |
    | `kw` | 标题关键词 | 按需使用 |
    | `start_time`/`end_time` | `YYYY:MM:DD` | ✅ **服务端真实生效** |
    | `timeType=6` | 使用自定义起止日期 | ✅ 必填，否则日期被忽略 |
    """
    return {
        "searchtype": "1",
        "page_index": str(page_index),
        "bidSort": "0",
        "buyerName": "",
        "projectId": "",
        "pinMu": str(pinmu),
        "bidType": str(bid_type),
        "dbselect": "bidx",
        "kw": keyword or "",
        "start_time": start_time,
        "end_time": end_time,
        "timeType": "6",
        "displayZone": "",
        "zoneId": "",
        "pppStatus": "0",
        "agentName": "",
    }


def parse_search_result(html: str) -> list[Notice]:
    """解析检索结果页。

    真实结构（已抓真实页面核实）：

        <div class="vT-srch-result-list-bid">
          <li>
            <a href="…/t20261003_27439571.htm">标题</a>
            <p></p>
            <span>2026.10.03 12:04:38 | 采购人：XXX | 代理机构：YYY
                  <br/><strong>更正公告</strong> | | <strong>（地区，常空）</strong>
            </span>
          </li>
          ...
        </div>

    注意：每个 `<li>` **同时**包住标题链接与元数据。
    """
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one(RESULT_SELECTOR)
    if container is None:
        return []

    notices: list[Notice] = []
    for li in container.find_all("li"):
        a = li.find("a")
        if a is None:
            continue
        title = a.get_text(strip=True)
        if is_junk(title):
            continue

        n = Notice(platform=PLATFORM_CCGP, title=title,
                   url=(a.get("href") or "").strip())

        span = li.find("span")
        if span is not None:
            txt = span.get_text(" ", strip=True)

            # 发布时间：点号分隔且精确到秒，例如 2026.10.03 12:04:38
            mt = re.search(r"\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}"
                           r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?", txt)
            n.publish_time = mt.group(0).strip() if mt else ""
            n.publish_date = to_iso_date(n.publish_time)

            # 采购人 / 代理机构：按中文标记取，不按位置
            mp = re.search(r"采购人\s*[：:]\s*([^|]+)", txt)
            n.purchaser = mp.group(1).strip() if mp else ""
            ma = re.search(r"代理机构\s*[：:]\s*([^|]+)", txt)
            n.agency = ma.group(1).strip() if ma else ""

            # 公告类型：第 1 个 <strong>，稳定可靠
            strongs = [s.get_text(strip=True) for s in span.find_all("strong")]
            n.category = strongs[0] if strongs else ""

            # 地区：不在 <strong> 内，是尾部纯文本（省份简称，常整体缺失）
            segs = [p.strip() for p in txt.split("|") if p.strip()]
            if len(segs) >= 4:
                cand = segs[-1]
                if (cand and cand != n.category and "公告" not in cand
                        and "采购人" not in cand and "代理机构" not in cand
                        and not re.match(r"^\d{4}[.\-/]", cand) and len(cand) <= 10):
                    n.area = cand

            # `<br/>` 不产生分隔符，会导致类型/地区粘在代理机构尾部
            for suffix in (n.category, n.area):
                if suffix and n.agency.endswith(suffix):
                    n.agency = n.agency[: -len(suffix)].strip()

        notices.append(n)
    return notices


def fetch_once(session: requests.Session, params: dict) -> tuple[str | None, str]:
    """发一次请求，返回 (正文, 状态码)。不做退避。"""
    try:
        resp = session.get(SEARCH_URL, params=params, headers=_headers(),
                           timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as e:
        return None, NETWORK

    text = decode_html(resp)
    if looks_blocked(text):
        return None, BLOCKED
    if RESULT_SELECTOR not in text:
        return None, STRUCTURE
    return text, "ok"


def fetch_page(session: requests.Session, params: dict,
               log=print) -> tuple[str | None, str]:
    return retry_call(lambda: fetch_once(session, params),
                      on_block_wait=BLOCK_WAIT,
                      max_block_retry=MAX_BLOCK_RETRY,
                      max_net_retry=MAX_NET_RETRY, log=log)


def collect(*, pages: int = 1, keyword: str = "", pinmu: str = "0",
            bid_type: str = "0", start_time: str = "", end_time: str = "",
            use_proxy: bool = False, delay: tuple[float, float] | None = None,
            log=print) -> list[Notice]:
    """采集多页，全局去重并做翻页完整性校验。"""
    session = build_session(use_proxy)
    lo, hi = delay or DEFAULT_DELAY
    found: list[Notice] = []
    seen: set[str] = set()
    raw_rows = 0
    pages_fetched = 0

    for page in range(1, pages + 1):
        log(f"\n=== 第 {page}/{pages} 页 ===")
        html, status = fetch_page(session, build_params(
            page, keyword, pinmu, bid_type, start_time, end_time), log=log)

        if html is None:
            log(f"[中止] 第 {page} 页取数失败：{status}")
            if status == BLOCKED:
                log("       该站点限流是短时的（约 60 秒恢复）。")
                log("       建议：调大 --delay（如 8-14），或换网络环境后重试。")
                log("       先用 `tender-notice check ccgp` 确认能通了再放大页数。")
            break

        items = parse_search_result(html)
        if not items:
            log("本页未解析到数据（可能已到末页，或筛选条件无结果）。")
            break

        pages_fetched += 1
        raw_rows += len(items)
        new = [x for x in items if norm_title(x.title) not in seen]
        for x in new:
            seen.add(norm_title(x.title))
        found.extend(new)

        log(f"本页解析 {len(items)} 条，新增 {len(new)} 条"
            f"（跳过重复 {len(items) - len(new)} 条）")

        if not new:
            log("[提示] 本页无新增 —— 判定已到末页或翻页未生效，停止。")
            break

        if page < pages:
            time.sleep(random.uniform(lo, hi))

    try:
        check_paging_integrity(raw_rows, len(found), pages_fetched)
    except Exception as e:
        log(f"\n[质量警告] {e}")

    return found


def check(use_proxy: bool = False, days: int = 3, log=print) -> bool:
    """连通性自检：一次请求内判断"能采／被限流／结构变了"。"""
    session = build_session(use_proxy)
    end = date.today()
    start = end - timedelta(days=days)
    params = build_params(1, "", "0", "0",
                          start.strftime("%Y:%m:%d"), end.strftime("%Y:%m:%d"))

    log(f"自检目标：{SEARCH_URL}")
    log(f"自检区间：{start} ~ {end}（近 {days} 天）")
    t0 = time.time()
    text, status = fetch_once(session, params)
    cost = time.time() - t0

    if text is not None:
        items = parse_search_result(text)
        log(f"\n[通过] 耗时 {cost:.1f}s，解析到 {len(items)} 条。")
        for it in items[:5]:
            log(f"   {it.title[:44]}  |  {it.publish_date}  |  {it.area}")
        return True

    log(f"\n[不通过] 耗时 {cost:.1f}s — 状态：{status}")
    if status == BLOCKED:
        log("被目标站点风控拦截（短时，通常约 60 秒恢复）。")
        log("请确认：① 是否误走了系统代理 ② 间隔是否过小 ③ 稍等后重试。")
    elif status == STRUCTURE:
        log("未找到结果容器 —— 站点页面结构可能已变更，需更新选择器。")
    elif status == NETWORK:
        log("网络异常：请检查网络连通性与是否需要代理。")
    return False


__all__ = [
    "SEARCH_URL", "RESULT_SELECTOR", "DEFAULT_DELAY",
    "build_params", "parse_search_result", "fetch_once", "fetch_page",
    "collect", "check",
]
