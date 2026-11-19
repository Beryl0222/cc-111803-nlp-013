"""把来源材料绑定到设备、事故和证明方向。

绑定是保守的：一条材料只有在文字能够指向某台设备时才为该设备提供
证明。同一句同时出现多台设备、只出现部件、或与登记对象冲突时，不
会强行拼接，而是保留疑问原文供经办人核对。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .extraction import (
    COMPONENT_WORDS,
    ONLY_ONE_REPAIRED_PATTERN,
    PER_DEVICE_SERVICE_PATTERN,
    SERIAL_PATTERN,
    SHARED_SERVICE_PATTERN,
    ClauseFacts,
    Extraction,
    extract_entities,
)


IDENTITY = '设备身份'
DAMAGE = '设备受损'
SCOPE = '维修范围'
SERVICE = '服务依据'
ATTRIBUTION = '服务归属'
ALLOCATION = '费用分摊'

DEVICE_ASPECTS = (IDENTITY, DAMAGE, SCOPE)
SERVICE_ASPECTS = (SERVICE, ATTRIBUTION, ALLOCATION)
ALL_ASPECTS = DEVICE_ASPECTS + SERVICE_ASPECTS

DAMAGE_KINDS = {'设备受损', '受损照片', '照片说明', '现场照片', '查勘照片'}
SCOPE_KINDS = {'维修范围', '维修单', '维修报告', '维修记录', '报价单', '报价', '分项报价'}
IDENTITY_KINDS = {'设备身份', '铭牌', '铭牌照片', '资产台账'}
SERVICE_KINDS = {'服务归属', '上门服务单', '服务单', '派工单'}
ALLOCATION_KINDS = {'费用分摊', '分项报价', '发票', '结算单'}

KIND_ASPECTS = {
    '设备受损': (DAMAGE,),
    '受损照片': (DAMAGE,),
    '照片说明': (DAMAGE,),
    '现场照片': (DAMAGE,),
    '查勘照片': (DAMAGE,),
    '维修范围': (SCOPE,),
    '维修单': (SCOPE,),
    '维修报告': (SCOPE,),
    '维修记录': (SCOPE,),
    '报价单': (SCOPE,),
    '报价': (SCOPE,),
    '设备身份': (IDENTITY,),
    '铭牌': (IDENTITY,),
    '铭牌照片': (IDENTITY,),
    '资产台账': (IDENTITY,),
    '服务归属': (SERVICE, ATTRIBUTION),
    '上门服务单': (SERVICE, ATTRIBUTION),
    '服务单': (SERVICE, ATTRIBUTION),
    '派工单': (SERVICE, ATTRIBUTION),
    '费用分摊': (ALLOCATION,),
    '分项报价': (SCOPE, ALLOCATION),
    '发票': (ALLOCATION,),
    '结算单': (ALLOCATION,),
}

SERVICE_KIND_WORDS = ('服务单', '上门单', '派工单')
DEVICE_SWITCH_WORDS = ('另一台', '另一', '第二台', '其余', '其他', '另有')
NEGATION_WORDS = (
    '未', '没有', '并未', '未能', '不', '无需', '无须', '未见', '未发现',
    '无', '正常', '完好', '不含', '不另',
)
REPAIR_WORDS = ('更换', '换下', '维修', '修理', '修复', '拆换', '替换', '加装')
ALLOCATION_WORDS = ('分摊', '各承担', '各支付', '每台', '折合', '分配', '分别收费', '分别计价')
UNSERVED_WORDS = ('未维修', '未修理', '未处理', '未上门', '未服务', '不需维修', '无需维修')
AMOUNT_SHARE_PATTERN = re.compile(
    r'(?:每台|各|分别)[^。；;，,\n]{0,12}?(\d[\d,]*(?:\.\d+)?)\s*元'
)


@dataclass(frozen=True)
class Doubt:
    document: str
    text: str
    reason: str
    device: str | None = None

    def to_dict(self):
        value = {'document': self.document, 'text': self.text, 'reason': self.reason}
        if self.device:
            value['device'] = self.device
        return value


def _dedupe(values):
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _serial_in(value) -> bool:
    return isinstance(value, str) and bool(SERIAL_PATTERN.search(value))


def _is_component(value, known=()) -> bool:
    if not isinstance(value, str):
        return False
    if value in known or _serial_in(value):
        return False
    return any(word == value or word in value for word in COMPONENT_WORDS)


def _has_negation(text: str, word: str) -> bool:
    position = text.find(word)
    if position < 0:
        return False
    prefix = text[max(0, position - 5):position]
    return any(negation in prefix for negation in NEGATION_WORDS)


def _amount_cents(value: str) -> int:
    return int(round(float(value.replace(',', '')) * 100))


@dataclass(frozen=True)
class ServiceObservation:
    document: str
    text: str
    incident: str | None
    devices: tuple[str, ...]
    quantity: int | None
    count: int | None
    explicit_count: bool
    per_device: bool
    shared: bool
    service_fee: bool
    allocation_unclear: bool
    allocation_documented: bool
    shared_quote: bool
    amounts_cents: tuple[int, ...]
    service_id: str | None = None
    site: str | None = None
    dates: tuple[str, ...] = ()
    only_one_repairs: tuple[str, ...] = ()
    doubtful: bool = False
    doubt: str | None = None

    @property
    def visits_supported(self) -> int | None:
        """这段文字能支持的访问次数；不能按设备数量凭空放大。"""

        if self.count is None:
            return None
        if self.per_device:
            # “两台设备各上门一次”是两次上门；原文写“分别/各”时才按
            # 设备台数放大。
            times = self.quantity or len(self.devices) or 1
            return self.count * times
        return self.count

    def covers(self, device) -> bool:
        return not self.devices or device in self.devices

    def covers_by_site(self, device, site) -> bool:
        """多台设备共用一次服务时，按同现场覆盖，但不能跨现场套用。"""

        if self.covers(device):
            return True
        if self.quantity and self.quantity > 1 and not self.per_device:
            return bool(site) and self.site == site
        return False

    def key(self, device=None, covers=()):
        """标识“哪一次上门”，用于发现同一服务被重复申报。"""

        base = self.service_id or self.document
        if self.shared:
            return ('shared', base, self.incident)
        if device:
            return ('visit', base, device, self.incident)
        if covers:
            return ('visit', base, tuple(sorted(covers)), self.incident)
        return ('visit', base, tuple(sorted(self.devices)), self.incident)

    def to_dict(self):
        return {
            'document': self.document,
            'text': self.text,
            'service_id': self.service_id,
            'site': self.site,
            'dates': list(self.dates),
            'only_one_repairs': list(self.only_one_repairs),
            'devices': list(self.devices),
            'quantity': self.quantity,
            'count': self.count,
            'visits_supported': self.visits_supported,
            'explicit_count': self.explicit_count,
            'shared': self.shared,
            'service_fee': self.service_fee,
            'allocation_unclear': self.allocation_unclear,
            'allocation_documented': self.allocation_documented,
            'shared_quote': self.shared_quote,
            'amounts_cents': list(self.amounts_cents),
            'doubtful': self.doubtful,
            'doubt': self.doubt,
        }


@dataclass
class Binding:
    document_id: str
    device: str | None = None
    incident: str | None = None
    site: str | None = None
    aspects: dict = field(default_factory=dict)
    doubts: list = field(default_factory=list)
    services: list = field(default_factory=list)
    quantity: int | None = None
    unknown_serials: tuple = ()
    components: tuple = ()
    only_one_repairs: tuple = ()
    damage_denials: tuple = ()
    scope_denials: tuple = ()
    declared_device: str | None = None

    def add(self, device, aspect, text, kind=None, component=None, reason=None):
        entry = {'document': self.document_id, 'text': text}
        if kind:
            entry['kind'] = kind
        if component:
            entry['component'] = component
        if reason:
            entry['reason'] = reason
        self.aspects.setdefault((device, aspect), []).append(entry)

    def has(self, device, aspect) -> bool:
        return bool(self.aspects.get((device, aspect)))

    def evidence(self, device, aspect):
        return list(self.aspects.get((device, aspect), []))

    def devices(self):
        return _dedupe(device for device, _ in self.aspects)

    def denials(self, device, aspect=()):
        if aspect == DAMAGE or (not aspect and DAMAGE):
            pool = self.damage_denials
        else:
            pool = self.scope_denials
        return [(item_device, text) for item_device, text in pool if item_device == device]

    def to_dict(self):
        return {
            'document': self.document_id,
            'declared_device': self.declared_device,
            'device': self.device,
            'incident': self.incident,
            'site': self.site,
            'aspects': {
                f'{device}:{aspect}': entries
                for (device, aspect), entries in self.aspects.items()
            },
            'doubts': [doubt.to_dict() for doubt in self.doubts],
            'services': [service.to_dict() for service in self.services],
            'quantity': self.quantity,
            'unknown_serials': list(self.unknown_serials),
            'components': list(self.components),
            'only_one_repairs': [
                {'device': device, 'text': text} for device, text in self.only_one_repairs
            ],
            'damage_denials': list(self.damage_denials),
            'scope_denials': list(self.scope_denials),
        }


def _clause_device(clause: ClauseFacts, known, declared, last_device):
    """返回（本句指向的设备, 解析方式）。解析方式用于生成疑问说明。"""

    serials = [serial for serial in clause.devices if serial in known]
    if len(serials) == 1:
        return serials[0], 'serial'
    if len(serials) > 1:
        return None, 'multi_serial'
    if clause.device_quantity and clause.device_quantity > 1:
        return None, 'quantity'
    if any(word in clause.text for word in DEVICE_SWITCH_WORDS):
        return None, 'switch'
    if declared:
        return declared, 'declared'
    if last_device:
        return last_device, 'continuation'
    return None, 'unknown'


def _allocation_documented(document, text) -> bool:
    allocation = document.get('allocation') or document.get('shares')
    if isinstance(allocation, (dict, list)) and allocation:
        return True
    if document.get('allocation_cents'):
        return True
    # 需要真正写出金额，不能拿序列号或日期里的数字充当分摊依据。
    return bool(AMOUNT_SHARE_PATTERN.search(text))


class EvidenceRegister:
    def __init__(self, devices, documents, default_incident=None):
        self.devices = {item['id']: item.get('site') for item in devices}
        self.documents = {item['id']: item for item in documents}
        self.default_incident = default_incident
        self._bindings: dict[str, Binding] = {}

    def binding(self, document_id):
        if document_id not in self._bindings:
            self._bindings[document_id] = self._build_binding(document_id)
        return self._bindings[document_id]

    def _document_incident(self, document, extraction):
        return (
            document.get('incident')
            or (extraction.incidents[0] if extraction.incidents else None)
            or self.default_incident
        )

    def _build_binding(self, document_id) -> Binding:
        document = self.documents[document_id]
        declared = document.get('device')
        declared_original = declared
        site = document.get('site')
        kind = document.get('kind')
        text = document.get('text') or ''
        binding = Binding(document_id, None, document.get('incident'), site)

        if declared is not None:
            if declared in self.devices:
                pass
            elif _is_component(declared, set(self.devices)):
                binding.doubts.append(Doubt(
                    document_id, str(declared),
                    f'“{declared}”是设备部件，不是独立设备，不能登记为第二台受损设备。',
                ))
                declared = None
            elif _serial_in(declared):
                binding.doubts.append(Doubt(
                    document_id, str(declared),
                    f'登记的设备 {declared} 不在设备目录中，无法确认对象身份。',
                ))
                declared = None
            else:
                binding.doubts.append(Doubt(
                    document_id, str(declared),
                    f'登记的设备 {declared} 不在设备目录中，无法确认对象身份。',
                ))
                declared = None

        extraction = (
            extract_entities(text)
            if isinstance(text, str) and text.strip()
            else Extraction()
        )
        binding.incident = self._document_incident(document, extraction)
        known_serials = [
            serial for serial in extraction.devices if serial in self.devices
        ]
        unknown = tuple(
            serial for serial in extraction.devices if serial not in self.devices
        )

        for serial in unknown:
            binding.doubts.append(Doubt(
                document_id, text,
                f'原文出现的序列号 {serial} 不在设备目录中，不能作为本项对象身份依据。',
            ))

        conflict = (
            declared is not None
            and known_serials
            and declared not in known_serials
        )
        if conflict:
            binding.doubts.append(Doubt(
                document_id, text,
                f'材料登记为 {declared}，但原文指向 {", ".join(known_serials)}，'
                '两者不能互指，未用于任何一台设备。',
            ))
            binding.device = None
            binding.unknown_serials = unknown
            binding.components = extraction.components
            binding.services = self._service_observations(document, extraction)
            binding.declared_device = declared_original
            return binding

        binding.declared_device = declared
        if declared is not None:
            # 文档自身登记的对象标记可以证明设备身份，但不能据此把整份
            # 材料的内容复制给另一台设备。
            binding.add(
                declared, IDENTITY,
                document.get('description') or kind or declared,
                kind=kind,
            )

        # 明确写出序列号的句子可以分别佐证对象身份。
        for clause in extraction.clauses:
            for serial in (
                serial for serial in clause.devices if serial in self.devices
            ):
                binding.add(serial, IDENTITY, clause.text, kind=kind)

        primary = declared
        if primary is None and len(known_serials) == 1:
            primary = known_serials[0]
        binding.device = primary

        damage_denials = []
        scope_denials = []
        only_one = []

        last_device = primary
        for clause in extraction.clauses:
            device, resolution = _clause_device(
                clause, set(self.devices), declared, last_device
            )
            if device is not None:
                last_device = device

            target = device or (
                primary if resolution in ('declared', 'continuation') else None
            )

            if clause.has_damage:
                damage_words = [
                    word for word in
                    ('进水', '损坏', '受损', '破损', '故障', '摔坏', '烧毁', '破裂', '异常', '泡水')
                    if word in clause.text
                ]
                positive = [
                    word for word in damage_words
                    if not _has_negation(clause.text, word)
                ]
                if positive and target:
                    binding.add(target, DAMAGE, clause.text, kind=kind)
                elif damage_words and not positive and target:
                    damage_denials.append((target, clause.text))
                elif positive:
                    self._add_unresolved_doubt(binding, document_id, clause, resolution)

            if clause.has_repair_scope:
                repair_words = [word for word in REPAIR_WORDS if word in clause.text]
                positive = [
                    word for word in repair_words
                    if not _has_negation(clause.text, word)
                ]
                if positive and target:
                    component = clause.components[0] if clause.components else None
                    binding.add(
                        target, SCOPE, clause.text, kind=kind, component=component
                    )
                elif repair_words and not positive and target:
                    scope_denials.append((target, clause.text))
                elif positive and target is None:
                    self._add_unresolved_doubt(
                        binding, document_id, clause, resolution
                    )

            if ONLY_ONE_REPAIRED_PATTERN.search(clause.text):
                serials = [
                    serial for serial in clause.devices if serial in self.devices
                ]
                if len(serials) == 1:
                    only_one.append((serials[0], clause.text))
                elif device is not None:
                    only_one.append((device, clause.text))

            if (
                not target
                and clause.devices
                and len(clause.devices) > 1
                and not ONLY_ONE_REPAIRED_PATTERN.search(clause.text)
                and (clause.has_damage or clause.has_repair_scope)
            ):
                self._add_unresolved_doubt(
                    binding, document_id, clause, 'multi_serial'
                )

        services = self._service_observations(document, extraction, only_one)
        binding.services = services
        binding.quantity = extraction.device_quantity
        binding.unknown_serials = unknown
        binding.components = extraction.components
        binding.only_one_repairs = tuple(only_one)

        # 同一句只写“两台同型主机…一次”而没有列明各自的维修项目：这句
        # 本身只能证明有一次上门服务，不能替任何一台证明维修范围。
        for clause in extraction.clauses:
            if (
                clause.device_quantity
                and clause.device_quantity > 1
                and clause.has_repair_scope
                and not [
                    serial for serial in clause.devices if serial in self.devices
                ]
            ):
                scope_denials.append((
                    '*',
                    clause.text,
                ))

        # “仅维修 Z91” 意味着同次服务覆盖的另一台没有维修范围。
        # “仅维修 Z91” 意味着同次服务覆盖的另一台没有维修范围。
        covered = _dedupe(
            [device for device, _ in only_one]
            + [device for observation in services for device in observation.devices]
            + list(binding.devices())
            + [serial for serial in extraction.devices if serial in self.devices]
        )
        for device, clause_text in only_one:
            for other in covered:
                if other != device and other in self.devices:
                    scope_denials.append((other, clause_text))

        binding.damage_denials = _dedupe(damage_denials)
        binding.scope_denials = _dedupe(scope_denials)

        self._add_service_aspects(
            binding, document, extraction, services, kind, primary
        )
        self._add_declared_aspects(
            binding, document, extraction, kind, primary, services
        )

        if (
            not text.strip()
            and declared is None
            and not extraction.devices
            and kind in (DAMAGE_KINDS | SCOPE_KINDS | IDENTITY_KINDS)
        ):
            binding.doubts.append(Doubt(
                document_id, str(document.get('description') or kind or ''),
                '材料没有登记设备，也没有可核对序列号，无法单独确认对象身份。',
            ))
        elif (
            text.strip()
            and declared is None
            and not extraction.devices
            and (extraction.has_damage or extraction.has_repair_scope)
            and not extraction.device_quantity
            and not any(
                doubt.device is None and '无法确认对象身份' in doubt.reason
                for doubt in binding.doubts
            )
        ):
            binding.doubts.append(Doubt(
                document_id, text,
                '原文只写“主机/设备”，没有可核对序列号；目录中有多台同型设备时无法确认对象身份。',
            ))

        binding.doubts = list({
            (d.document, d.text, d.reason, d.device): d
            for d in binding.doubts
        }.values())
        return binding

    def _add_unresolved_doubt(self, binding, document_id, clause, resolution):
        reasons = {
            'multi_serial': '同一句同时出现多台设备，不能确认受损或维修对象。',
            'quantity': (
                f'原文写“{clause.device_quantity} 台”设备，却没有列明各自序列号，'
                '无法确认这一句分别指向哪一台。'
            ),
            'switch': '原文提到“另一台/其他设备”，但没有写明序列号，无法确认所指对象。',
            'unknown': '原文只写“主机/设备”，没有可核对序列号，无法确认对象身份。',
        }
        reason = reasons.get(
            resolution, '原文未写明设备序列号，无法确认对象身份。'
        )
        binding.doubts.append(Doubt(document_id, clause.text, reason))

    def _service_observations(self, document, extraction, only_one=()) -> list:
        text = document.get('text') or ''
        kind = document.get('kind')
        if not (
            extraction.has_service
            or extraction.service_fee
            or kind in SERVICE_KINDS
        ):
            return []

        per_device = bool(PER_DEVICE_SERVICE_PATTERN.search(text))
        quantity = extraction.device_quantity
        shared = bool(SHARED_SERVICE_PATTERN.search(text)) or (
            quantity is not None and quantity > 1 and not per_device
        )
        count = extraction.service_count
        explicit = extraction.service_explicit
        if count is None and extraction.service_fee:
            count = 1
            explicit = False

        device = document.get('device')
        if _is_component(device, set(self.devices)) or device not in self.devices:
            device = None
        devices = _dedupe(
            [serial for serial in extraction.devices if serial in self.devices]
            + ([device] if device else [])
        )
        # “本次上门只维修 Z91，Z19 未维修”：服务对象只覆盖被维修的那台。
        if only_one and any(word in text for word in UNSERVED_WORDS):
            repaired = _dedupe(device_id for device_id, _ in only_one)
            restricted = tuple(
                device_id for device_id in devices if device_id in repaired
            )
            if restricted:
                devices = restricted
        doubtful = not devices and not document.get('service_id') and not document.get('visit_id')
        doubt = None
        doubt = None
        if doubtful:
            doubt = (
                '原文未写可核对的设备序列号，服务对象只能按同型台数推定，'
                '需服务单确认。'
            )

        allocation_unclear = extraction.allocation_unclear
        allocation_documented = (
            _allocation_documented(document, text)
            and not allocation_unclear
        )
        service_id = document.get('service_id') or document.get('visit_id')
        return [ServiceObservation(
            document=document['id'],
            text=text,
            incident=(
                document.get('incident')
                or (extraction.incidents[0] if extraction.incidents else None)
                or self.default_incident
            ),
            devices=devices,
            quantity=quantity,
            count=count,
            explicit_count=explicit,
            per_device=per_device,
            shared=shared,
            service_fee=extraction.service_fee,
            allocation_unclear=allocation_unclear,
            allocation_documented=allocation_documented,
            shared_quote=extraction.shared_quote,
            amounts_cents=extraction.amounts_cents,
            service_id=service_id,
            site=(
                document.get('site')
                or (self.devices.get(devices[0]) if devices else None)
            ),
            dates=extraction.dates,
            only_one_repairs=tuple(
                device_id for device_id, _ in only_one
            ),
            doubtful=doubtful,
            doubt=doubt,
        )]

    def _add_service_aspects(
        self, binding, document, extraction, services, kind, primary
    ):
        for observation in services:
            targets = observation.devices or (
                [primary] if primary else []
            )
            for target in targets:
                if observation.doubtful and not observation.service_id:
                    continue
                binding.add(target, SERVICE, observation.text, kind=kind)
                attribution_text = observation.text
                if observation.service_id:
                    attribution_text = (
                        f'{observation.text}（服务单号 {observation.service_id}）'
                    )
                binding.add(
                    target, ATTRIBUTION, attribution_text, kind=kind
                )
                if observation.allocation_documented:
                    binding.add(
                        target, ALLOCATION, observation.text, kind=kind
                    )
            if observation.doubtful:
                binding.doubts.append(Doubt(
                    document['id'], observation.text, observation.doubt,
                ))
            if observation.allocation_unclear:
                binding.doubts.append(Doubt(
                    document['id'], observation.text,
                    '材料写明未列上门费分配，共用服务的金额无法落到具体设备。',
                    primary,
                ))

    def _add_declared_aspects(
        self, binding, document, extraction, kind, primary, services
    ):
        """按材料类别补充证明方向，但维修范围必须有原文动作支持。

        有正文的维修单如果只写“两台同型主机上门服务一次”，不能因为
        类别是维修单就替任何一台设备证明维修范围。
        """

        if primary is None:
            return
        wanted = (
            document.get('supports')
            or document.get('aspects')
            or KIND_ASPECTS.get(kind, ())
        )
        if not isinstance(wanted, (list, tuple)):
            wanted = (wanted,)
        if (
            kind
            and any(word in kind for word in SERVICE_KIND_WORDS)
            and SERVICE not in wanted
        ):
            wanted = tuple(wanted) + (SERVICE,)

        metadata_only = not (document.get('text') or '').strip()
        for aspect in wanted:
            if aspect not in ALL_ASPECTS:
                continue
            if binding.has(primary, aspect):
                continue
            if aspect == IDENTITY:
                binding.add(
                    primary, IDENTITY,
                    document.get('description') or kind or primary,
                    kind=kind,
                )
            elif aspect == DAMAGE:
                denied = binding.denials(primary, DAMAGE)
                if denied:
                    # 照片说明或正文明确写“完好/未见进水”时，不能仅凭
                    # 材料类别就当作受损证据。
                    binding.doubts.append(Doubt(
                        document['id'], denied[0][1],
                        '原文表明该设备未见受损，与“设备受损”材料类别相反，'
                        '不能作为受损依据。',
                        primary,
                    ))
                elif metadata_only or kind in DAMAGE_KINDS:
                    binding.add(
                        primary, DAMAGE,
                        document.get('description') or kind or primary,
                        kind=kind,
                    )
            elif aspect == SCOPE:
                # 维修范围只能由维修动作原文或纯结构化材料证明。
                if metadata_only:
                    binding.add(
                        primary, SCOPE,
                        document.get('description') or kind or primary,
                        kind=kind,
                    )
            elif aspect in (SERVICE, ATTRIBUTION):
                if metadata_only and any(
                    service.covers(primary) for service in services
                ):
                    binding.add(
                        primary, aspect,
                        document.get('description') or kind or primary,
                        kind=kind,
                    )
            elif aspect == ALLOCATION:
                if metadata_only or any(
                    service.allocation_documented for service in services
                ):
                    binding.add(
                        primary, ALLOCATION,
                        document.get('description') or kind or primary,
                        kind=kind,
                    )

    def effective_incident(self, document_id):
        binding = self.binding(document_id)
        return binding.incident or self.default_incident

    def supports_aspect(self, document_id, device, incident, aspect):
        """返回（依据列表, 不能组合的原因）。"""

        if document_id not in self.documents:
            return None, {
                'document': document_id,
                'code': 'unknown_document',
                'reason': '材料未登记。',
            }
        item = self.documents[document_id]
        binding = self.binding(document_id)

        if device not in self.devices:
            return None, {
                'document': document_id,
                'code': 'unknown_device',
                'reason': f'设备 {device} 不在设备目录中。',
            }

        if item.get('site') and item.get('site') != self.devices[device]:
            return None, {
                'document': document_id,
                'code': 'site_mismatch',
                'reason': (
                    f'材料现场为 {item.get("site")}，与 {device} 所在 '
                    f'{self.devices[device]} 不一致。'
                ),
                'text': item.get('text') or item.get('description'),
            }

        material_incident = self.effective_incident(document_id)
        if incident and material_incident and material_incident != incident:
            return None, {
                'document': document_id,
                'code': 'incident_mismatch',
                'reason': (
                    f'材料记载事故为 {material_incident}，与本项 {incident} 不一致。'
                ),
                'text': item.get('text') or item.get('description'),
            }
        if incident and not material_incident:
            return None, {
                'document': document_id,
                'code': 'incident_unknown',
                'reason': '材料未记载事故编号，无法确认与本次事故为同一事故。',
                'text': item.get('text') or item.get('description'),
            }

        entries = binding.evidence(device, aspect)
        denials = binding.damage_denials if aspect == DAMAGE else (
            binding.scope_denials if aspect == SCOPE else ()
        )
        device_denials = [
            text for denied_device, text in denials
            if denied_device == device
        ]
        if entries and device_denials:
            return None, {
                'document': document_id,
                'code': 'conflict',
                'reason': (
                    f'材料对 {device} 的{aspect}存在相反表述，不能直接作为'
                    '已完成的结论。'
                ),
                'text': item.get('text') or item.get('description'),
                'conflict': {
                    'supported': entries,
                    'denied': device_denials,
                },
            }
        if entries:
            # “两台同型主机上门服务一次”只能说明服务，不能替任何一台
            # 证明纳入了维修范围。
            ambiguous = [
                text for denied_device, text in denials
                if denied_device == '*' and aspect == SCOPE
            ]
            if ambiguous and not any(
                entry.get('component') for entry in entries
            ):
                return None, {
                    'document': document_id,
                    'code': 'scope_unspecified',
                    'reason': (
                        '原文只写多台同型设备的上门服务，未写明各自的维修'
                        '项目，不能证明本机维修范围。'
                    ),
                    'text': ambiguous[0],
                    'doubt': {
                        'document': document_id,
                        'text': ambiguous[0],
                        'reason': (
                            '多台设备共用的维修记录没有逐台列明维修项目，'
                            '不能把同一项目复制给每一台。'
                        ),
                    },
                }
            return entries, None

        other_devices = [
            other for other in binding.devices()
            if other != device and binding.has(other, aspect)
        ]
        if other_devices:
            return None, {
                'document': document_id,
                'code': 'other_device',
                'reason': (
                    f'材料指向 {", ".join(sorted(other_devices))}，'
                    f'不能作为 {device} 的{aspect}依据。'
                ),
                'text': item.get('text') or item.get('description'),
                'devices': sorted(other_devices),
            }

        doubt = next(
            (d for d in binding.doubts if not d.device or d.device == device),
            None,
        )
        reason = {
            'document': document_id,
            'code': 'missing_aspect',
            'reason': f'材料未证明{aspect}。',
            'text': item.get('text') or item.get('description'),
        }
        if binding.device is None and not binding.aspects and binding.doubts:
            # 材料登记对象与原文指向冲突或无法互指：明确指出是哪份文字
            # 产生疑问，而不是笼统写“未证明”。
            first = binding.doubts[0]
            reason['code'] = 'unbound'
            reason['reason'] = first.reason
            reason['text'] = first.text
            reason['doubt'] = first.to_dict()
        elif device_denials:
            reason['code'] = 'denied'
            reason['reason'] = (
                f'材料表明 {device} 未{aspect}或已被排除。'
            )
            reason['denied_by'] = device_denials
        if doubt:
            reason['doubt'] = doubt.to_dict()
        return None, reason

    def supports(self, document_id, device, incident):
        """兼容旧接口：返回材料类别或证明方向。"""

        if document_id not in self.documents:
            return None
        binding = self.binding(document_id)
        for aspect in ALL_ASPECTS:
            if binding.has(device, aspect):
                return self.documents[document_id].get('kind') or aspect
        return None

    def service_observations(self, document_id=None):
        if document_id is not None:
            return list(self.binding(document_id).services)
        result = []
        for current in self.documents:
            result.extend(self.binding(current).services)
        return result

    def doubts(self, devices=None, incident=None):
        result = []
        seen = set()
        for document_id in self.documents:
            if incident and self.effective_incident(document_id) not in (None, incident):
                continue
            for doubt in self.binding(document_id).doubts:
                if devices and doubt.device and doubt.device not in devices:
                    continue
                value = (doubt.document, doubt.text, doubt.reason, doubt.device)
                if value in seen:
                    continue
                seen.add(value)
                result.append(doubt.to_dict())
        return result
