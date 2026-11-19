"""从报案、照片说明和报价原文中抽取理赔对象与事实线索。

这里只做保守的规则抽取。抽取结果用于把不同来源的材料绑定到同一事故和
同一对象，或者在无法绑定时指出产生疑问的原文。部件（例如外壳）始终只
记录为部件，不会被当成一台独立设备。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .text import normalize_text


SERIAL_PATTERN = re.compile(r'(?<![A-Za-z0-9])([A-Z]{1,4}-?\d{2,})(?![A-Za-z0-9])')
INCIDENT_PATTERN = re.compile(r'((?:CL|CASE|INC)[-_]?\d+)', re.IGNORECASE)
AMOUNT_PATTERN = re.compile(r'(\d[\d,]*(?:\.\d+)?)\s*(?:元|块)')
SERVICE_COUNT_PATTERN = re.compile(
    r'(?:上门服务|上门维修|上门检查|上门处理|上门|到场)'
    r'[^。；;，,\n]{0,12}?([一二两三四五六七八九十0-9]+)\s*次'
)
SERVICE_VERB_PATTERN = re.compile(
    r'(?:上门|到场)\s*(?:服务|维修|修理|检查|检测|处理|更换|拆装|查看)'
)
SERVICE_FEE_PATTERN = re.compile(r'(?:上门费|服务费|差旅费|出场费)')
DEVICE_QUANTITY_PATTERN = re.compile(
    r'([一二两三四五六七八九十0-9]+)\s*(?:台|件|套)\s*'
    r'(?:同型|同型号|相同型号)?\s*(?:主机|设备|机器|电脑)'
)
DEVICE_QUANTITY_TRAILING_PATTERN = re.compile(
    r'(?:同型|同型号|相同型号)?\s*(?:主机|设备|机器|电脑)\s*'
    r'([一二两三四五六七八九十0-9]+)\s*(?:台|件|套)'
)
SHARED_QUOTE_PATTERN = re.compile(r'(?:两台|多台|合并|一并).{0,8}报价')
ALLOCATION_UNCLEAR_PATTERN = re.compile(r'未[^。；;，,\n]{0,8}(?:分配|分摊)')
ONLY_ONE_REPAIRED_PATTERN = re.compile(
    r'(?:只|仅|单独|其中只有)[^。；;，,\n]{0,12}(?:维修|修理|更换|修复|处理|修)'
)
PER_DEVICE_SERVICE_PATTERN = re.compile(r'(?:各|分别|各自)[^。；;，,\n]{0,6}(?:上门|到场)')
SHARED_SERVICE_PATTERN = re.compile(r'(?:同次|同一次|一次上门|共同|合并|一共)')
DATE_PATTERN = re.compile(
    r'(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日?'
    r'|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}'
    r'|\d{1,2}\s*月\s*\d{1,2}\s*日)'
)
CLAUSE_SPLIT_PATTERN = re.compile(r'[。；;，\n]')

# 部件词只代表设备的一部分，不能作为第二台设备登记。
COMPONENT_WORDS = (
    '主机外壳', '外壳', '机壳', '主板', '面板', '屏幕', '显示屏', '电池',
    '风扇', '压缩机', '摄像头', '键盘', '底座', '接口', '电源',
)

DAMAGE_WORDS = (
    '进水', '水迹', '受潮', '损坏', '受损', '破损', '故障', '摔坏', '烧毁',
    '破裂', '异常', '泡水', '锈蚀',
)
# “更换、维修”等才说明修了什么；“拆下、送修、检查”本身不足以证明维修范围。
REPAIR_SCOPE_WORDS = ('更换', '换下', '维修', '修理', '修复', '拆换', '替换', '加装')
INSPECTION_WORDS = ('检查', '检测', '查看', '排查')
HANDLING_WORDS = ('拆下', '送修', '带走', '取走')
SERVICE_WORDS = ('上门', '到场')

_NUMERALS = {
    '零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
    '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
}


def _number(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    if token in _NUMERALS:
        return _NUMERALS[token]
    return None


def _amount_cents(value: str) -> int:
    return int(round(float(value.replace(',', '')) * 100))


def _dedupe(values):
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _components_in(text: str) -> tuple[str, ...]:
    hits = []
    for word in COMPONENT_WORDS:
        position = text.find(word)
        if position >= 0:
            hits.append((position, -len(word), word))
    ordered = [word for _, _, word in sorted(hits)]
    # “主机外壳”已经涵盖“外壳”，不能把同一部件记成两个。
    return _dedupe(
        word for word in ordered
        if not any(word != other and word in other for other in ordered)
    )


def split_clauses(text: str) -> list[str]:
    """按中文句读切分。ASCII 逗号可能是 58,000 的千位分隔，不能切。"""

    return [part.strip() for part in CLAUSE_SPLIT_PATTERN.split(text) if part.strip()]


@dataclass(frozen=True)
class ClauseFacts:
    text: str
    devices: tuple[str, ...] = ()
    components: tuple[str, ...] = ()
    incidents: tuple[str, ...] = ()
    amounts_cents: tuple[int, ...] = ()
    device_quantity: int | None = None
    service_count: int | None = None
    service_explicit: bool = False
    service_fee: bool = False
    has_damage: bool = False
    has_repair_scope: bool = False
    has_inspection: bool = False
    has_handling: bool = False
    service_scope: str | None = None
    only_one_repaired: bool = False
    dates: tuple[str, ...] = ()

    def to_dict(self):
        return {
            'text': self.text,
            'devices': list(self.devices),
            'components': list(self.components),
            'incidents': list(self.incidents),
            'amounts_cents': list(self.amounts_cents),
            'device_quantity': self.device_quantity,
            'service_count': self.service_count,
            'service_explicit': self.service_explicit,
            'service_scope': self.service_scope,
            'service_fee': self.service_fee,
            'dates': list(self.dates),
            'has_damage': self.has_damage,
            'has_repair_scope': self.has_repair_scope,
            'has_inspection': self.has_inspection,
            'has_handling': self.has_handling,
            'only_one_repaired': self.only_one_repaired,
        }


@dataclass(frozen=True)
class Extraction:
    devices: tuple[str, ...] = ()
    components: tuple[str, ...] = ()
    incidents: tuple[str, ...] = ()
    amounts_cents: tuple[int, ...] = ()
    service_counts: tuple[int, ...] = ()
    service_explicit: bool = False
    service_fee: bool = False
    device_quantity: int | None = None
    has_damage: bool = False
    has_repair_scope: bool = False
    has_inspection: bool = False
    has_handling: bool = False
    shared_quote: bool = False
    allocation_unclear: bool = False
    dates: tuple[str, ...] = ()
    clauses: tuple[ClauseFacts, ...] = ()
    text: str = ''

    @property
    def has_service(self) -> bool:
        return bool(self.service_counts)

    @property
    def service_count(self) -> int | None:
        return max(self.service_counts) if self.service_counts else None

    def to_dict(self):
        return {
            'devices': list(self.devices),
            'components': list(self.components),
            'incidents': list(self.incidents),
            'amounts_cents': list(self.amounts_cents),
            'service_counts': list(self.service_counts),
            'service_count': self.service_count,
            'service_explicit': self.service_explicit,
            'service_fee': self.service_fee,
            'device_quantity': self.device_quantity,
            'has_damage': self.has_damage,
            'has_repair_scope': self.has_repair_scope,
            'has_inspection': self.has_inspection,
            'has_handling': self.has_handling,
            'shared_quote': self.shared_quote,
            'allocation_unclear': self.allocation_unclear,
            'dates': list(self.dates),
            'clauses': [clause.to_dict() for clause in self.clauses],
        }


def extract_clause(text: str) -> ClauseFacts:
    service_count = None
    service_explicit = False
    match = SERVICE_COUNT_PATTERN.search(text)
    if match:
        service_count = _number(match.group(1))
        service_explicit = True
    elif SERVICE_VERB_PATTERN.search(text) or any(
        word in text for word in SERVICE_WORDS
    ):
        # “同次上门检查”“本次上门只维修 Z91”只描述了一次服务动作，
        # 按一次计，但不是原文明写的次数。
        service_count = 1

    quantity = None
    match = DEVICE_QUANTITY_PATTERN.search(text) or DEVICE_QUANTITY_TRAILING_PATTERN.search(text)
    if match:
        quantity = _number(match.group(1))

    return ClauseFacts(
        text=text,
        devices=_dedupe(match.upper() for match in SERIAL_PATTERN.findall(text)),
        components=_components_in(text),
        incidents=_dedupe(match.upper() for match in INCIDENT_PATTERN.findall(text)),
        amounts_cents=tuple(_amount_cents(match) for match in AMOUNT_PATTERN.findall(text)),
        device_quantity=quantity,
        service_count=service_count,
        service_explicit=service_explicit,
        service_fee=bool(SERVICE_FEE_PATTERN.search(text)),
        has_damage=any(word in text for word in DAMAGE_WORDS),        has_repair_scope=any(word in text for word in REPAIR_SCOPE_WORDS),
        has_inspection=any(word in text for word in INSPECTION_WORDS),
        has_handling=any(word in text for word in HANDLING_WORDS),
        dates=_dedupe(re.sub(r'\s+', '', value) for value in DATE_PATTERN.findall(text)),
    )


def extract_entities(text: str) -> Extraction:
    """抽取设备序列号、部件、事故、金额和服务次数。

    ``devices`` 只包含 ``Z91`` 这样的设备序列号；只提到“外壳”时，
    ``devices`` 为空，部件记录在 ``components``，因此不会被算作第二台
    受损设备。
    """

    if not isinstance(text, str):
        raise TypeError('文本必须是字符串')

    normalized = normalize_text(text)
    clauses = tuple(extract_clause(part) for part in split_clauses(normalized))

    return Extraction(
        devices=_dedupe(device for clause in clauses for device in clause.devices),
        components=_dedupe(component for clause in clauses for component in clause.components),
        incidents=_dedupe(incident for clause in clauses for incident in clause.incidents),
        amounts_cents=tuple(amount for clause in clauses for amount in clause.amounts_cents),
        service_counts=_dedupe(
            clause.service_count for clause in clauses if clause.service_count is not None
        ),
        service_explicit=any(clause.service_explicit for clause in clauses),
        service_fee=any(clause.service_fee for clause in clauses),
        device_quantity=max(
            (clause.device_quantity for clause in clauses if clause.device_quantity is not None),
            default=None,
        ),
        has_damage=any(clause.has_damage for clause in clauses),
        has_repair_scope=any(clause.has_repair_scope for clause in clauses),
        has_inspection=any(clause.has_inspection for clause in clauses),
        has_handling=any(clause.has_handling for clause in clauses),
        shared_quote=bool(SHARED_QUOTE_PATTERN.search(normalized)),
        allocation_unclear=bool(ALLOCATION_UNCLEAR_PATTERN.search(normalized)),
        dates=_dedupe(date for clause in clauses for date in clause.dates),
        clauses=clauses,
        text=normalized,
    )
