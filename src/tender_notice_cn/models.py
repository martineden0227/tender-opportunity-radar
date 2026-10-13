"""统一数据模型。

两个平台的原始字段名、粒度都不同（一个返回 HTML 列表、一个返回 JSON），
本模块定义**采集层的统一结构**，让下游（合并、导表、分析）不必关心来源差异。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


# 平台标识
PLATFORM_CCGP = "ccgp"   # 中国政府采购网（政府采购体系）
PLATFORM_CEB = "ceb"     # 中国招标投标公共服务平台（依法必须招标项目）

PLATFORM_LABEL = {
    PLATFORM_CCGP: "中国政府采购网",
    PLATFORM_CEB: "中国招标投标公共服务平台",
}

# 公告类型：属"研究竞争对手"环节，不进"机会级"候选池
RESULT_KEYWORDS = (
    "中标公告", "中标结果", "中标公示", "中标候选人", "候选人公示",
    "成交公告", "成交结果", "结果公告", "结果更正", "结果公示",
    "废标", "终止公告", "流标", "开标记录",
)


@dataclass
class Notice:
    """一条招标公告（线索的最小单元 = 一条公告）。"""

    platform: str = ""            # 平台标识：ccgp / ceb
    title: str = ""               # 公告标题
    url: str = ""                 # 详情入口
    publish_time: str = ""        # 发布时间（原样保留来源格式）
    publish_date: str = ""        # 归一化日期 YYYY-MM-DD
    purchaser: str = ""           # 采购人 / 招标人
    agency: str = ""              # 代理机构
    area: str = ""                # 地区
    category: str = ""            # 公告类型（公开招标／竞争性磋商／更正…）
    industry: str = ""            # 所属专业 / 品目
    deadline: str = ""            # 公告结束时间（经逻辑校验）
    project_code: str = ""        # 项目代码
    platform_name: str = ""       # 所属交易平台（平台二有此字段）
    issues: str = ""              # 数据质量标注
    extra: dict = field(default_factory=dict)

    # ---------------- 派生属性 ----------------

    @property
    def is_result_notice(self) -> bool:
        """是否为中标/成交类公告（不进机会级候选池）。"""
        hay = f"{self.title} {self.category}"
        return any(w in hay for w in RESULT_KEYWORDS)

    @property
    def source_label(self) -> str:
        return PLATFORM_LABEL.get(self.platform, self.platform)

    def to_row(self, index: int, retrieve_date: str) -> dict:
        """转换为候选池的一行（13 列规格）。"""
        who = " / ".join(x for x in (self.purchaser, self.agency) if x)
        extra_bits = [f"发布时间={self.publish_date}"]
        if self.category:
            extra_bits.append(f"公告类型={self.category}")
        if self.industry:
            extra_bits.append(f"所属专业={self.industry}")
        if self.project_code:
            extra_bits.append(f"项目代码={self.project_code}")
        if self.platform_name:
            extra_bits.append(f"所属交易平台={self.platform_name}")
        if self.issues:
            extra_bits.append(f"数据质量={self.issues}")

        return {
            "序号": index,
            "成熟度": "机会级",
            "项目名称": self.title,
            "标段／招标内容": "列表页未列明，需点入详情确认",
            "采购人／代理机构": who or "列表页未列明，需点入详情确认",
            "区域": self.area,
            "预算·限价": "列表页未列明，需点入详情确认",
            "投标截止": self.deadline or "以平台日程安排为准",
            "总判定": "待确认",
            "来源平台": f"全国级·{self.source_label}",
            "详情入口": self.url,
            "检索日期": retrieve_date,
            "备注": "；".join(extra_bits),
        }

    def as_dict(self) -> dict:
        return asdict(self)


POOL_COLUMNS = [
    "序号", "成熟度", "项目名称", "标段／招标内容", "采购人／代理机构", "区域",
    "预算·限价", "投标截止", "总判定", "来源平台", "详情入口", "检索日期", "备注",
]

__all__ = [
    "Notice", "PLATFORM_CCGP", "PLATFORM_CEB", "PLATFORM_LABEL",
    "POOL_COLUMNS", "RESULT_KEYWORDS",
]
