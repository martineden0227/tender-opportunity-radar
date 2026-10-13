"""适配器二：中国招标投标公共服务平台（依法必须招标项目）。

实测要点（2026-10 复核，详见 docs/platform-notes.md）：

* 可用入口是**门户主站下的信息公开检索接口**，返回 **JSON**
  `{"success": true, "object": {"returnlist": [...], "page": {...}}}`。
  （该子路径不受另一子域的路径级 WAF 拦截，两者规则不同。）

* **三条硬约束**：
  1. **翻页不可用** —— `pageNo>=2` 返回验证码校验失败（`v-token` 为空）。
     **本库不逆向、不绕过验证码**，改用 `row` 放大单页容量（实测 `row=500` 正常）。
  2. **日期与地区筛选无效** —— `searchArea` / `searchTimeStart` / `searchTimeStop` /
     `timeTypeParam` 提交后返回结果**完全不变**，服务端忽略。
     只有 `searchName`（关键词）与 `searchIndustry`（行业分类）真正生效。
  3. **字段空值占位** —— `bulletinEndTime` 存在 `1970-01-01`，须识别后置空。

* ⚠️ **由此引出的口径边界（必须写进交付物）**：
  因日期筛选无效，采集只能做「**关键词分片 × 全库 → 本地按接收时间筛**」。
  产出的**不是"某时段全量"，而是"该时段内命中关键词的子集"**。
  不清楚这条边界的使用者会误以为拿到了全量。

* `receiveTime` 是「**接收时间**」，含**提前发布与预告**，会晚于当天
  （实测出现过晚于检索日 27 天的记录）。投标前须回源站确认公告状态。
"""

from __future__ import annotations

import json
import re
import time

import requests

from .common import (
    BLOCKED, DEFAULT_TIMEOUT, DEFAULT_UA, NETWORK, STRUCTURE,
    build_session, clean_date, dedup, looks_blocked, norm_title, to_iso_date,
)
from .models import PLATFORM_CEB, Notice

BASE = "https://www.cebpubservice.com"
API = BASE + "/ctpsp_iiss/searchbusinesstypebeforedooraction/getStringMethod.do"
REFERER = BASE + "/ctpsp_iiss/searchbusinesstypebeforedooraction/getSearch.do"
DETAIL_URL = BASE + "/ctpsp_iiss/search/bulletin/searchBulletinDetail.do?bulletinId={}"

MAX_ROW = 500                 # 实测上限；再大无收益

# 公告类型由请求参数决定（接口不返回类型字段）。
# 取"招标公告"= 只采机会类公告；改成"中标结果公示"等即改为采结果类。
BUSINESS_TYPE = "招标公告"
DEFAULT_DELAY = 1.5
BLOCK_WAIT = 90.0
MAX_BLOCK_RETRY = 3
MAX_NET_RETRY = 4

# 关键词分片：正式名称 + 行业俗称 + 上下游别名
# 词根来源：《国民经济行业分类》GB/T 4754—2017、《建筑业企业资质标准》类别名
DEFAULT_KEYWORDS = [
    # 工程类
    "房屋建筑", "市政公用", "公路", "桥梁", "隧道", "水利水电", "电力工程",
    "机电安装", "钢结构", "装饰装修", "幕墙", "地基基础", "环保", "消防",
    "城市及道路照明", "园林绿化", "土地整治", "测绘", "地质勘察", "路灯",
    # 货物类
    "设备采购", "设备", "材料", "物资", "电梯", "空调", "电缆", "管道",
    "泵站", "阀门", "配电", "监控", "仪器仪表", "车辆采购", "家具", "图书",
    # 服务类
    "咨询服务", "服务", "运维", "物业管理", "保洁", "绿化养护", "安保",
    "培训", "审计", "造价咨询", "检测", "勘察设计", "监理", "检测服务",
]


def _headers() -> dict:
    return {
        "User-Agent": DEFAULT_UA,
        "Referer": REFERER,
        "Origin": BASE,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "*/*",
    }


def build_payload(*, keyword: str = "", industry: str = "", business: str = "招标公告",
                  page: int = 1, row: int = MAX_ROW,
                  area: str = "", start: str = "", stop: str = "") -> dict:
    """构造请求体。

    注意：`area`/`start`/`stop` 会被服务端忽略（保留参数仅为兼容与留痕）。
    """
    return {
        "searchName": keyword,
        "searchArea": area,
        "searchIndustry": industry,
        "centerPlat": "",
        "businessType": business,
        "searchTimeStart": start,
        "searchTimeStop": stop,
        "timeTypeParam": "",
        "bulletinIssnTime": "",
        "bulletinIssnTimeStart": "",
        "bulletinIssnTimeStop": "",
        "pageNo": str(page),
        "row": str(row),
    }


def parse_records(records) -> list[Notice]:
    """把接口返回的 JSON 记录转成统一结构。

    字段映射（均为实测字段名，取不到就留空，**绝不用推断值填充**）：

    | 接口字段 | 含义 |
    |---|---|
    | `businessObjectName` | 公告标题 |
    | `industriesType` | 所属专业 |
    | `regionName` | 所属区域 |
    | `transactionPlatfName` | 所属交易平台 |
    | `receiveTime` | **接收时间**（含提前发布/预告） |
    | `bulletinEndTime` | 公告结束时间（有空值占位） |
    | `tenderProjectCode` | 项目代码 |
    | `businessId` | 业务 ID（拼详情链接用） |
    """
    def g(rec, *keys):
        for k in keys:
            v = rec.get(k)
            if v not in (None, "", "null"):
                return re.sub(r"\s+", " ", str(v)).strip()
        return ""

    out: list[Notice] = []
    for rec in records or []:
        title = g(rec, "businessObjectName", "tenderBulletinName", "bulletinName")
        if not title:
            continue

        deadline, issue = clean_date(g(rec, "bulletinEndTime"))
        bid = rec.get("businessId") or ""

        n = Notice(
            platform=PLATFORM_CEB,
            title=title,
            url=DETAIL_URL.format(bid) if bid else "",
            publish_time=g(rec, "receiveTime"),
            publish_date=to_iso_date(g(rec, "receiveTime")),
            area=g(rec, "regionName"),
            industry=g(rec, "industriesType"),
            platform_name=g(rec, "transactionPlatfName"),
            # 接口**不返回**公告类型字段，类型由请求参数 businessType 决定 ——
            # 本适配器恒以 businessType=招标公告 请求，故类型即"招标公告"。
            # （实测返回字段全集里无任何类型字段，故不能从记录中取。）
            category=BUSINESS_TYPE,
            deadline=deadline,
            project_code=g(rec, "tenderProjectCode"),
            issues=issue,
        )
        out.append(n)
    return out


def fetch_once(session: requests.Session, payload: dict) -> tuple[str | None, str, list | None]:
    """发一次请求。返回 (正文, 状态, 记录列表)。"""
    try:
        resp = session.post(API, data=payload, headers=_headers(),
                            timeout=DEFAULT_TIMEOUT)
    except requests.RequestException:
        return None, NETWORK, None

    body = resp.content.decode("utf-8", "ignore")
    if looks_blocked(body):
        return None, BLOCKED, None

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None, STRUCTURE, None

    if not data.get("success"):
        msg = str(data.get("message") or "")
        # 翻页被验证码拦住 —— 这是已知限制，不是网络问题
        if "验证码" in msg or "v-token" in msg:
            return None, BLOCKED, None
        return None, STRUCTURE, None

    records = (data.get("object") or {}).get("returnlist") or []
    return body, "ok", records


def check(use_proxy: bool = False, log=print) -> bool:
    """连通性自检（单次请求）。"""
    session = build_session(use_proxy)
    log(f"自检目标：{API}")
    t0 = time.time()
    body, status, records = fetch_once(session, build_payload(keyword=""))
    cost = time.time() - t0

    if records is not None:
        items = parse_records(records)
        log(f"\n[通过] 耗时 {cost:.1f}s，解析到 {len(items)} 条"
            f"（单页 row={MAX_ROW}）。")
        for it in items[:5]:
            log(f"   {it.title[:44]}  |  {it.publish_date}  |  {it.area}")
        log("\n注意：该平台**翻页不可用**（需验证码 token），"
            "只能靠关键词分片扩大覆盖。")
        return True

    log(f"\n[不通过] 耗时 {cost:.1f}s — 状态：{status}")
    if status == BLOCKED:
        log("被风控拦截。若为翻页请求被拦属已知限制（本库不使用翻页）。")
    elif status == STRUCTURE:
        log("接口返回结构可能已变更，或请求头不完整（需 Referer/Origin）。")
    elif status == NETWORK:
        log("网络异常：请检查连通性与代理设置。")
    return False


def collect(*, start: str = "", stop: str = "", keywords: list[str] | None = None,
            industry: str = "", row: int = MAX_ROW, delay: float = DEFAULT_DELAY,
            use_proxy: bool = False, log=print) -> list[Notice]:
    """关键词分片采集，并按接收时间在**本地**筛选区间。

    参数
    ----
    start / stop
        `YYYY-MM-DD`。因服务端忽略日期参数，筛选在本地按 `receiveTime` 进行。
        **留空则不筛**（返回全部命中）。
    keywords
        分片关键词；默认使用内置业务词表。词表越贴合业务范围，覆盖越准。
    """
    session = build_session(use_proxy)
    words = keywords if keywords is not None else DEFAULT_KEYWORDS
    gathered: list[Notice] = []
    seen: set[str] = set()
    raw_rows = 0          # 各分片返回的原始行数合计
    slice_dups = 0        # 跨分片重复行数

    log(f"=== 关键词分片采集（{len(words)} 片，每片 row={row}）===")
    log("说明：服务端忽略日期与地区筛选，时间窗将在本地按「接收时间」筛选。\n")

    for i, kw in enumerate(words, 1):
        payload = build_payload(keyword=kw, industry=industry, row=row)

        def _once(_p=payload):
            body, status, records = fetch_once(session, _p)
            return (("ok", records) if records is not None else (None, status))

        # 轻量退避（分片之间本身已有间隔，故用较短退避）
        attempt = 0
        records = None
        while attempt < 3:
            res, payload_or_status = _once()
            if res == "ok":
                records = payload_or_status
                break
            attempt += 1
            if payload_or_status == BLOCKED:
                log(f"  [{i:2}/{len(words)}] {kw:12} 被限流，等待 {BLOCK_WAIT:.0f}s …")
                time.sleep(BLOCK_WAIT)
            elif payload_or_status == NETWORK:
                time.sleep(3 * attempt)
            else:
                break

        if records is None:
            log(f"  [{i:2}/{len(words)}] {kw:12} 取数失败，跳过")
            if delay:
                time.sleep(delay)
            continue

        items = parse_records(records)
        raw_rows += len(items)
        new = 0
        for n in items:
            k = norm_title(n.title, 44)
            if k in seen:
                slice_dups += 1
                continue
            seen.add(k)
            gathered.append(n)
            new += 1

        log(f"  [{i:2}/{len(words)}] {kw:12} 返回 {len(items):3} 条 | 新增 {new:3} 条"
            f" | 累计 {len(gathered)}")
        if delay:
            time.sleep(delay)

    # ⚠️ 这里**不做**翻页完整性校验：分片之间出现重复是**设计使然** ——
    # 同一条公告会在多个关键词下被命中（例如标题同时含"电梯"与"设备采购"）。
    # 该判据只适用于"按页码翻页"的采集方式（见 ccgp 适配器）。
    if raw_rows:
        log(f"\n跨分片去重：{raw_rows} → {len(gathered)}（剔除重复 {slice_dups} 行）")
        log("  说明：同一公告会在多个关键词下命中，跨分片重复属正常现象，不是翻页失效。")

    # 本地时间窗筛选
    if start or stop:
        before = len(gathered)
        gathered = [n for n in gathered
                    if n.publish_date and (not start or n.publish_date >= start)
                    and (not stop or n.publish_date <= stop)]
        log(f"\n本地时间窗筛选（{start or '起'} ~ {stop or '止'}）："
            f"{before} → {len(gathered)} 条")
        log("⚠️ 这是「命中关键词的子集」，不等于该时段的全量公告。")

    return dedup(gathered)


__all__ = [
    "BASE", "API", "DETAIL_URL", "MAX_ROW", "DEFAULT_KEYWORDS",
    "build_payload", "parse_records", "fetch_once", "collect", "check",
]
