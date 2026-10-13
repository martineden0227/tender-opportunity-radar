"""共用工具：会话、编码、限流识别、重试分类、数据质量校验。

本模块把两个平台采集器的**共性部分**抽出来，避免每个适配器各写一份。
所有方法论均来自对两个平台的实测复核，注释里标注了"为什么这么写"。
"""

from __future__ import annotations

import re
import time
from typing import Callable, Iterable, Sequence

import requests

# --------------------------------------------------------------------------
# 一、会话与网络路径
# --------------------------------------------------------------------------

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 25


def build_session(use_proxy: bool = False, user_agent: str = DEFAULT_UA) -> requests.Session:
    """建立会话。

    ⚠️ 实测踩坑（最隐蔽的一个，曾导致整轮排查方向错误）：

    本机常会开启系统代理并写入 **Windows 注册表**
    （`HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings`）。
    `requests` 会通过 `urllib.request.getproxies()` **自动读取注册表代理**，
    **仅 `unset http_proxy` 环境变量摘不掉它** —— 环境变量已清空，注册表仍在。

    后果：请求被导向一个本机代理，若该代理出口被目标站点风控标记，
    表现为"**永远**拿到拦截页"，极易被误判为"本机 IP 被封"，
    从而在"怎么绕限流"上反复试错。

    本库面向境内政务站点，默认**直连**（`trust_env=False` + `proxies={}`）。
    需要走代理时显式传 `use_proxy=True`。

    排查提示：抓不到数据时，先跑 `python -c "import urllib.request;
    print(urllib.request.getproxies())"` 确认实际网络路径，再怀疑目标站点。
    """
    s = requests.Session()
    if use_proxy:
        s.trust_env = True
    else:
        s.trust_env = False     # 不读环境变量、不读注册表代理
        s.proxies = {}          # 显式清空，双保险
    s.headers.update({
        "User-Agent": user_agent,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    return s


def decode_html(resp: requests.Response) -> str:
    """稳健解码：HTTP 头 charset → 页面 meta charset → utf-8 → gbk → gb18030。

    为什么不能直接用 `resp.text`：HTTP 头不带 charset 时，requests 回落到
    ISO-8859-1，中文会变乱码。而**乱码会让后续的关键词匹配静默失效** ——
    不报错、只给空结果，使用者会误以为"页面结构变了"而去改选择器。

    为什么不能只靠 `resp.apparent_encoding`：它基于 chardet 特征探测，
    页面字节数较少（例如拦截页）时可能猜错。
    """
    raw = resp.content
    candidates: list[str] = []

    m = re.search(r"charset=([\w-]+)", resp.headers.get("Content-Type", ""), re.I)
    if m:
        candidates.append(m.group(1).lower())

    head = raw[:4096].decode("ascii", "ignore")
    for pat in (r'charset=["\']?([\w-]+)', r'encoding=["\']?([\w-]+)'):
        m = re.search(pat, head, re.I)
        if m:
            candidates.append(m.group(1).lower())

    candidates += ["utf-8", "gbk", "gb18030"]

    seen: set[str] = set()
    for enc in candidates:
        if enc in seen:
            continue
        seen.add(enc)
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if text.count("\ufffd") > 5:      # 替换字符过多 → 猜错编码，试下一个
            continue
        return text
    return raw.decode("utf-8", "ignore")


# 被限流的页面特征
BLOCK_MARKERS = ("频繁访问", "过于频繁", "访问被阻断", "访问过于频繁")


def looks_blocked(text: str) -> bool:
    """判断返回的是否为风控拦截页。

    ⚠️ 注意：这类拦截通常返回 **HTTP 200** 而不是 403，
    所以**不能只看状态码**，必须检查正文特征。
    """
    return any(m in text for m in BLOCK_MARKERS)


# 重试分类
OK = "ok"
BLOCKED = "blocked"       # 被限流：长退避后重试有意义
NETWORK = "network"       # 瞬时网络抖动：短退避重试
STRUCTURE = "structure"   # 页面结构变更：重试无意义


def retry_call(
    func: Callable[[], tuple[str | None, str]],
    *,
    on_block_wait: float = 90.0,
    max_block_retry: int = 3,
    max_net_retry: int = 4,
    log: Callable[[str], None] = print,
) -> tuple[str | None, str]:
    """带分类退避的重试。

    实测教训：曾把瞬时 `Read timed out` 与"被限流"同等对待，
    更早的版本甚至把非限流错误直接放弃 —— 结果采集在第 7 页就中断。
    两类失败必须**分开处理**。
    """
    block_used = net_used = 0
    while block_used < max_block_retry and net_used < max_net_retry:
        text, status = func()
        if text is not None:
            return text, OK

        if status == BLOCKED:
            block_used += 1
            log(f"[限流] 等待 {on_block_wait:.0f}s 后重试（第 {block_used}/{max_block_retry} 次）…")
            time.sleep(on_block_wait)
        elif status == NETWORK:
            net_used += 1
            wait = 3 * net_used
            log(f"[网络] 瞬时异常，{wait}s 后重试（第 {net_used}/{max_net_retry} 次）…")
            time.sleep(wait)
        else:
            return None, status

    if net_used >= max_net_retry:
        return None, f"网络异常重试 {net_used} 次仍失败"
    return None, "多次重试仍被限流"


# --------------------------------------------------------------------------
# 二、数据质量校验（本库最重要的纪律）
# --------------------------------------------------------------------------

JUNK_KEYWORDS = (
    "招标公告名称", "中标公告名称", "采购公告名称",
    "暂无", "更多", "首页", "下一页", "上一页", "末页", "条记录",
)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def norm_title(title: str, limit: int = 50) -> str:
    """标题归一化，用于跨页/跨批次去重。"""
    return re.sub(r"[\s\u3000]+", "", title or "")[:limit]


def is_junk(title: str) -> bool:
    """过滤表头行、导航行与提示行。

    结果容器里"表头"也是普通单元格，不排掉就会产出标题为"招标公告名称"的假记录。
    """
    t = (title or "").strip()
    if not t or len(t) < 6:
        return True
    return any(k in t for k in JUNK_KEYWORDS)


def clean_date(value: str, publish: str = "") -> tuple[str, str]:
    """校验端点日期。返回 (清洗后的值, 问题说明)。

    实测检出过的脏值（不会报错，但会被一路带进投标决策与报价测算）：
        发布=2026-09-30  截止=2025-10-10   ← 截止早于发布一年
        发布=2026-09-16  截止=2026-09-14   ← 截止早于发布 2 天
    另有一种空值占位：`1970-01-01`。

    规则：非日期格式、或"截止早于发布"的值一律置空，并返回问题说明。
    """
    v = (value or "").strip()
    if not v:
        return "", ""
    if v.startswith("1970-01-01"):
        return "", "截止时间为空值占位(1970-01-01)，已置空"
    if not DATE_RE.match(v):
        m = re.search(r"\d{4}-\d{2}-\d{2}", v)
        if not m:
            return "", f"截止时间非日期格式({v[:20]})，已置空"
        v = m.group(0)
    if publish and DATE_RE.match(publish) and v < publish:
        return "", f"截止早于发布({publish}→{v})，已置空"
    return v, ""


def to_iso_date(s: str) -> str:
    """把 `2026.10.03 12:04:38` / `2026/10/3` 等写法归一化为 `2026-10-03`。"""
    m = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", s or "")
    if not m:
        return ""
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


class PagingIntegrityError(RuntimeError):
    """翻页失效（假数据）时抛出。"""


def check_paging_integrity(raw_count: int, unique_count: int, pages: int = 1,
                           *, max_dup_ratio: float = 1.0) -> None:
    """翻页完整性校验 —— **本项目最重要的质量闸**。

    实测已发生两起"假成功"：

    | 程序报出的结果 | 真实情况 |
    |---|---|
    | "6 页 96 条" | 96 行只有 **15 个唯一标题**，每条重复 6 次（重复次数 = 页数） |
    | "45 条" | 只有 **14 个唯一标题**，每条重复 3 次 |

    假翻页的指纹：**重复行数 ≥ 唯一条数**（极端情形是"每页完全相同"，
    此时 `重复行数 = 唯一条数 × (页数-1)`）。
    成因通常是目标站点要求验证码／签名 token，而请求未携带。

    参数
    ----
    max_dup_ratio
        允许的重复比例（相对唯一条数）。默认 `1.0`，即重复行数不得超过唯一条数。
        **留出这个口子是因为少量跨页边界重复属正常现象**
        （同一公告被重复发布、或分页边界重叠），不应一律判为失败；
        早期版本无条件抛错，会把正常结果误判为失败。

    只做校验与报警，**不做静默修补** —— 宁可少给数据，也不拿重复数据充数。
    """
    if raw_count <= 0:
        return
    dup = raw_count - unique_count
    if dup <= 0:
        return
    if unique_count == 0:
        raise PagingIntegrityError(
            f"原始 {raw_count} 行全部被判为无效/重复，无有效数据。")
    if dup < max(3.0, unique_count * max_dup_ratio):
        return                      # 轻微重复：正常，放行

    msg = (f"检出大量重复数据：原始 {raw_count} 行，唯一 {unique_count} 条，"
           f"重复 {dup} 行。")
    if pages > 1:
        per_page = unique_count
        if abs(dup - per_page * (pages - 1)) <= max(per_page * 0.2, 2):
            msg += (f" 重复量 ≈ 每页条数 × (页数-1)（{per_page} × {pages - 1}），"
                    f"**高度疑似翻页未生效**：各页返回了同一批数据。")
    msg += " 建议：先核对目标站点的翻页机制是否被风控拦截（验证码／签名 token）。"
    raise PagingIntegrityError(msg)


def dedup(items: Iterable, key: Callable | None = None) -> list:
    """按归一化标题全局去重，保序。"""
    key = key or (lambda x: norm_title(getattr(x, "title", "")))
    seen: set[str] = set()
    out = []
    for it in items:
        k = key(it)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def sequence_identical(a: Sequence, b: Sequence) -> bool:
    """判断两页是否完全相同（判定"翻页未生效"的快速信号）。"""
    return list(a) == list(b)


def today_iso() -> str:
    from datetime import date
    return date.today().isoformat()


__all__ = [
    "build_session", "decode_html", "looks_blocked", "retry_call",
    "OK", "BLOCKED", "NETWORK", "STRUCTURE",
    "norm_title", "is_junk", "clean_date", "to_iso_date",
    "check_paging_integrity", "PagingIntegrityError", "dedup",
    "sequence_identical", "today_iso", "JUNK_KEYWORDS", "DATE_RE",
    "DEFAULT_UA", "DEFAULT_TIMEOUT", "BLOCK_MARKERS",
]
