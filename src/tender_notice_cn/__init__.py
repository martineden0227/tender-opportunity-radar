"""tender-notice-cn —— 中国招标公告采集工具。

覆盖两个全国级公开渠道：

* **中国政府采购网**（政府采购体系）—— 日期筛选与翻页在服务端真实生效
* **中国招标投标公共服务平台**（依法必须招标项目）—— 翻页需验证码，日期筛选无效，
  故采用「关键词分片 × 单页大容量 + 本地时间筛」

用法::

    from tender_notice_cn import ccgp, ceb, pool

    items = ccgp.collect(pages=3, start_time="2026:10:01", end_time="2026:10:03")
    rows, stats = pool.merge(items)

命令行::

    tender-notice --help

合规立场（见 docs/compliance.md）：

* 只采集**公开**信息；不获取、不使用任何非公开信息。
* **不绕过**验证码、登录墙等访问控制。遇到风控即降速、等待或改用人工。
* 不承诺"自动全网抓取"；各站点公开深度不同，能力边界在文档中如实标注。
* 采集结果**必须回源站复核**；本工具输出的是线索与入口，不是结论。
"""

__version__ = "0.1.2"

from .models import Notice, PLATFORM_CCGP, PLATFORM_CEB  # noqa: F401
from . import ccgp, ceb, pool  # noqa: F401

__all__ = ["Notice", "PLATFORM_CCGP", "PLATFORM_CEB", "ccgp", "ceb", "pool", "__version__"]
