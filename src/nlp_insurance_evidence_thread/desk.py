"""核赔范围核对：把材料绑定到费用项并分别判断各证明方向。

核心约束：

* 设备身份、设备受损、维修范围可以分别由不同材料证明，但只有指向同一
  设备、同一事故时才组合；
* 共用一次上门服务按“一次”计，不能按台数重复核赔；
* 已出决定保留，补充材料只触发复核并指出具体部分；
* 不同缺口发出不同的补件请求。
"""

from __future__ import annotations

from dataclasses import dataclass

from .evidence import (
    ALLOCATION,
    ATTRIBUTION,
    DAMAGE,
    IDENTITY,
    SCOPE,
    SERVICE,
    EvidenceRegister,
)


GAP_REQUESTS = {
    IDENTITY: {
        'documents': ['铭牌照片', '资产台账或设备清单'],
        'ask': '请补充能显示该设备序列号的铭牌照片或资产台账，并注明事故编号。',
    },
    DAMAGE: {
        'documents': ['现场照片', '查勘记录'],
        'ask': '请补充能显示该设备受损部位和序列号的现场照片或查勘记录。',
    },
    SCOPE: {
        'documents': ['维修单', '维修报告', '列明维修项目的分项报价'],
        'ask': (
            '请补充列明该设备序列号和实际维修项目（含更换部件）的维修单或'
            '分项报价；只写“上门服务”不能证明维修范围。'
        ),
    },
    SERVICE: {
        'documents': ['上门服务单', '派工单'],
        'ask': '请补充写明上门日期、上门次数和所涉设备序列号的服务单或派工单。',
    },
    ATTRIBUTION: {
        'documents': ['上门服务单', '派工单'],
        'ask': '请补充能确认该次上门服务归属于哪些设备的服务单或派工单。',
    },
    ALLOCATION: {
        'documents': ['分项报价', '结算单'],
        'ask': '请补充按设备列明上门服务费分摊金额的报价或结算单，避免同一服务重复核赔。',
    },
}

SERVICE_TEXT_WORDS = ('上门', '服务费', '差旅', '出场', '到场', '派工')
SERVICE_ITEM_WORDS = ('上门', '服务费', '差旅', '出场', '到场', '派工')
DEVICE_REQUIRED_ASPECTS = (IDENTITY, DAMAGE, SCOPE)


def _dedupe(entries):
    seen = set()
    result = []
    for entry in entries:
        key = (
            entry.get('document'), entry.get('text'),
            entry.get('kind'), entry.get('component'), entry.get('reason'),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(entry)
    return result


def _unique(values):
    seen = set()
    result = []
    for value in values:
        if isinstance(value, dict):
            key = tuple(
                (name, _freeze(item)) for name, item in sorted(value.items())
            )
        else:
            key = value
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _freeze(value):
    if isinstance(value, dict):
        return tuple(sorted((name, _freeze(item)) for name, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _is_service_item(item) -> bool:
    if item.get('service') is True:
        return True
    if item.get('kind') == '服务':
        return True
    text = f'{item.get("kind", "")}{item.get("description", "")}'
    return any(word in text for word in SERVICE_ITEM_WORDS)


def _cost_service_items(cost) -> list:
    return [item for item in cost.get('items', []) if _is_service_item(item)]


def _cost_device_items(cost) -> list:
    return [item for item in cost.get('items', []) if not _is_service_item(item)]


def _cost_claims_service(cost, request, observations) -> bool:
    if _cost_service_items(cost):
        return True
    if cost.get('service') is True or cost.get('kind') == '服务':
        return True
    text = f'{cost.get("kind", "")}{cost.get("description", "")}'
    if any(word in text for word in SERVICE_TEXT_WORDS):
        return True
    if cost.get('service_cents') or request.get('includes_service') or cost.get('includes_service'):
        return True
    for observation in observations:
        if observation.service_fee:
            return True
    return False


def _observation_applies(observation, cost) -> bool:
    device = cost.get('device')
    if device is None:
        return True
    if observation.devices:
        return device in observation.devices
    return True


def _visit_key(observation, cost, register) -> tuple:
    """同一服务单号优先；否则同一事故、同一现场归入同一组。

    不能按设备拆分：多台设备共用的一次上门如果按设备各成一组，就会
    把“一次服务”算成两次，正好造成重复核赔。不同上门日期在组内单独
    计数，见 ``_group_service_state``。
    """

    incident = observation.incident
    if observation.service_id:
        return ('service_id', observation.service_id, incident)
    if observation.per_device and observation.devices:
        return ('per_device', observation.document, cost.get('device'), incident)
    device = cost.get('device')
    site = observation.site
    if site is None and device in register.devices:
        site = register.devices.get(device)
    return ('visit', incident, site)


@dataclass
class ServiceClaim:
    cost_id: str
    request_documents: tuple
    observations: tuple

    @property
    def visit_count(self) -> int:
        total = 0
        for observation in self.observations:
            count = observation.visits_supported
            total += count if count is not None else 1
        return total


class CostReview:
    def __init__(self, register, costs, default_incident=None):
        self.register = register
        self.costs = {cost['id']: cost for cost in costs}
        self.default_incident = default_incident

    def classify(self, request):
        """兼容旧接口：返回（费用, 缺失证明方向）。"""

        cost = self.costs[request['cost']]
        detail = self.detail(request)
        return cost, detail['missing']
    def detail(self, request):
        cost = self.costs[request['cost']]
        device = cost.get('device')
        incident = request.get('incident') or self.default_incident
        aspects = DEVICE_REQUIRED_ASPECTS if device is not None else ()
        evidence = {aspect: [] for aspect in aspects}
        reasons = {aspect: [] for aspect in aspects}

        documents = list(request.get('documents') or [])
        for document_id in documents:
            for aspect in aspects:
                entries, why = self.register.supports_aspect(
                    document_id, device, incident, aspect
                )
                if entries:
                    evidence[aspect].extend(entries)
                elif why:
                    reasons[aspect].append(why)

        missing = [aspect for aspect in aspects if not evidence[aspect]]
        gaps = []
        for aspect in missing:
            gap = self._gap(cost, device, incident, aspect, documents, reasons[aspect])
            gaps.append(gap)

        service_gaps = self._request_service_gaps(cost, documents, incident)

        return {
            'cost': cost,
            'device': device,
            'incident': incident,
            'documents': documents,
            'evidence': {aspect: _dedupe(values) for aspect, values in evidence.items()},
            'missing': missing,
            'gaps': gaps,
            'service_gaps': service_gaps,
            'reasons': reasons,
        }

    def _request_service_gaps(self, cost, documents, incident):
        """本项材料是否足以说明共用服务归属到这台设备。"""

        if cost.get('device') is None:
            # 纯服务费费用项本身不绑定单台设备，由服务次数核对处理。
            return []
        if not _cost_claims_service(cost, {'cost': cost['id']}, []):
            return []
        observations = []
        for document_id in documents:
            for observation in self.register.service_observations(document_id):
                if incident and observation.incident and observation.incident != incident:
                    continue
                if _observation_applies(observation, cost):
                    observations.append(observation)
        if not observations:
            return []
        device = cost.get('device')
        site = self.register.devices.get(device)
        # 写明服务单号、明确写了本机序列号，或已列出分摊金额，都足以
        # 确认这次上门与本机的关系。
        if any(
            not observation.doubtful and device in observation.devices
            for observation in observations
        ):
            return []
        if any(
            observation.service_id
            and observation.covers_by_site(device, site)
            for observation in observations
        ):
            return []
        if any(
            observation.allocation_documented
            and observation.covers_by_site(device, site)
            for observation in observations
        ):
            return []
        return [{
            'aspect': SERVICE,
            'code': 'service_unproven',
            'reason': '服务记录未写明服务单号、维修对象或分摊金额，无法确认该次上门归属本机。',
            'request': dict(GAP_REQUESTS[SERVICE]),
        }]

    def _gap(self, cost, device, incident, aspect, documents, reasons):
        template = GAP_REQUESTS.get(aspect, {
            'documents': ['情况说明'],
            'ask': f'请补充能证明{aspect}的材料。',
        })
        chosen = _choose_reason(reasons, documents)
        gap = {
            'cost': cost['id'],
            'device': device,
            'aspect': aspect,
            'code': chosen.get('code', 'missing_document'),
            'reason': chosen.get('reason', f'本项未证明{aspect}。'),
            'request': {
                'documents': list(template['documents']),
                'ask': template['ask'],
            },
        }
        for key in ('document', 'text', 'doubt', 'devices', 'conflict', 'denied_by'):
            if chosen.get(key) is not None:
                gap[key] = chosen[key]
        if chosen.get('code') in (None, 'missing_aspect') and not chosen.get('document'):
            if documents:
                gap['document'] = documents[0]
                gap['text'] = self._document_text(documents[0])
            else:
                gap['reason'] = f'本项未提交任何材料，无法证明{aspect}。'
        gap['incident'] = incident
        return gap

    def _document_text(self, document_id):
        document = self.register.documents.get(document_id, {})
        return document.get('text') or document.get('description')


def _choose_reason(reasons, documents):
    priority = {
        'conflict': 0,
        'denied': 1,
        'other_device': 2,
        'multiple_devices': 3,
        'site_mismatch': 4,
        'incident_mismatch': 5,
        'incident_unknown': 6,
        'unknown_document': 7,
        'missing_aspect': 8,
        'unknown_device': 9,
        'missing_document': 10,
    }
    if reasons:
        return sorted(
            reasons,
            key=lambda reason: (
                priority.get(reason.get('code'), 20),
                0 if reason.get('doubt') else 1,
            ),
        )[0]
    return {}


class SettlementScope:
    """保存已出决定。原决定只读，补充材料不得覆盖。"""

    def __init__(self, review, decided):
        if isinstance(decided, dict):
            self.decided = dict(decided)
        else:
            self.decided = {decision['cost']: decision for decision in decided}
        self.review = review


def _service_claims(review, requests):
    claims = []
    for request in requests:
        cost = review.costs.get(request['cost'])
        if cost is None:
            continue
        observations = []
        for document_id in request.get('documents') or []:
            for observation in review.register.service_observations(document_id):
                incident = request.get('incident') or review.default_incident
                if observation.incident and incident and observation.incident != incident:
                    continue
                if _observation_applies(observation, cost):
                    observations.append(observation)
        if observations and _cost_claims_service(cost, request, observations):
            claims.append(ServiceClaim(
                cost_id=cost['id'],
                request_documents=tuple(request.get('documents') or []),
                observations=tuple(observations),
            ))
    return claims


def _service_groups(review, requests):
    """按“哪一次上门”归并服务记录，无法区分的同场服务按一次处理。"""

    groups = {}
    for claim in _service_claims(review, requests):
        cost = review.costs[claim.cost_id]
        for observation in claim.observations:
            key = _visit_key(observation, cost, review.register)
            group = groups.setdefault(key, {
                'key': key,
                'observations': [],
                'claimed_by': [],
                'devices': [],
                'incident': observation.incident,
            })
            group['observations'].append(observation)
            group['claimed_by'] = _unique(
                group['claimed_by'] + [claim.cost_id]
            )
            group['devices'] = _unique(
                group['devices'] + list(observation.devices)
                + ([cost['device']] if cost.get('device') else [])
            )
    return _drop_superseded_groups(list(groups.values()))


def _drop_superseded_groups(groups):
    """补上的服务单明确同一次上门时，旧的泛化描述不再另算一次。

    只丢弃没有服务单号、且与已识别服务单在同一事故、同一现场、覆盖
    同一设备（或没有写明日期冲突）的泛化记录。写了不同日期的泛化记录
    仍保留，避免把两次上门压成一次。
    """

    identified = [
        group for group in groups
        if group['key'][0] == 'service_id'
    ]
    if not identified:
        return groups
    kept = []
    for group in groups:
        if group['key'][0] == 'service_id':
            kept.append(group)
            continue
        observation = group['observations'][0]
        generic_dates = {
            date for item in group['observations'] for date in item.dates
        }
        superseded = False
        for other in identified:
            other_observation = other['observations'][0]
            if other['incident'] != observation.incident:
                continue
            if other_observation.site != observation.site:
                continue
            if not (set(other['devices']) & set(group['devices'])):
                continue
            other_dates = {
                date for item in other['observations'] for date in item.dates
            }
            if generic_dates and other_dates and not (generic_dates & other_dates):
                continue
            superseded = True
            break
        if not superseded:
            kept.append(group)
    return kept


def _group_service_state(group):
    observations = group['observations']
    reliable = [observation for observation in observations if not observation.doubtful]
    supported = 0
    for observation in reliable or observations:
        count = observation.visits_supported
        supported = max(supported, count if count is not None else 0)
    # 同组内出现两个不同的上门日期，说明至少是两次上门，即使每份材料
    # 各只写了一次，也不能压成一次。
    date_sets = {
        tuple(sorted(observation.dates))
        for observation in reliable
        if observation.dates
    }
    if len(date_sets) > 1:
        supported = max(supported, len(date_sets))
    any_service_id = any(observation.service_id for observation in reliable)
    allocation_documented = any(
        observation.allocation_documented for observation in reliable
    )
    allocation_unclear = any(
        observation.allocation_unclear for observation in reliable
    )
    doubtful = not reliable
    claimed = len(group['claimed_by'])

    if supported <= 0:
        state = '待补服务单'
    elif doubtful:
        state = '待补服务单'
    elif claimed > 1 and supported <= 1 and not allocation_documented:
        state = '重复申报'
    elif claimed > supported and not allocation_documented:
        state = '待分摊'
    elif allocation_unclear and not allocation_documented:
        state = '待分摊'
    else:
        state = '可核赔一次' if supported == 1 else '可核赔'
    return {
        'state': state,
        'supported_visits': supported,
        'claimed_visits': claimed,
        'allocation_documented': allocation_documented,
        'any_service_id': any_service_id,
        'doubtful': doubtful,
    }


def _service_reviews(review, requests):
    reviews = []
    for group in _service_groups(review, requests):
        assessment = _group_service_state(group)
        observations = group['observations']
        reviews.append({
            'key': list(group['key']),
            'document': observations[0].document,
            'text': observations[0].text,
            'documents': _unique([observation.document for observation in observations]),
            'devices': list(group['devices']),
            'claimed_by': list(group['claimed_by']),
            **assessment,
            'basis': _unique([
                {
                    'document': observation.document,
                    'text': observation.text,
                    'service_id': observation.service_id,
                    'count': observation.count,
                }
                for observation in observations
            ]),
        })
    return reviews


def _cost_service_view(cost_id, service_reviews):
    relevant = [review for review in service_reviews if cost_id in review['claimed_by']]
    if not relevant:
        return {'claimed': False, 'state': '不涉及', 'gaps': []}
    states = [review['state'] for review in relevant]
    if '重复申报' in states:
        state = '重复申报'
    elif '待补服务单' in states:
        state = '待补服务单'
    elif '待分摊' in states:
        state = '待分摊'
    else:
        supported = max(
            (review.get('supported_visits') or 0) for review in relevant
        )
        state = '可核赔一次' if supported <= 1 else '可核赔'
    gaps = []
    if state == '重复申报':
        gaps.append({
            'aspect': SERVICE,
            'code': 'duplicate_service',
            'reason': '同一上门服务被多项费用引用，且没有分摊凭据，不能重复核赔。',
            'request': dict(GAP_REQUESTS[ATTRIBUTION]),
        })
    elif state == '待补服务单':
        gaps.append({
            'aspect': SERVICE,
            'code': 'service_unproven',
            'reason': '服务记录未写服务单号或无法确认该次上门具体服务了哪些设备。',
            'request': dict(GAP_REQUESTS[SERVICE]),
        })
    if state == '待分摊':
        gaps.append({
            'aspect': ALLOCATION,
            'code': 'allocation_missing',
            'reason': '共用一次上门服务，但报价未列明各设备分摊金额。',
            'request': dict(GAP_REQUESTS[ALLOCATION]),
        })
    for gap in gaps:
        gap['document'] = relevant[0].get('document')
        gap['text'] = relevant[0].get('text')
    return {
        'claimed': True,
        'state': state,
        'shared': any(review.get('claimed_visits') and review['claimed_visits'] > 1 for review in relevant)
        or any(len(review.get('devices') or []) > 1 for review in relevant),
        'service_basis': [
            {
                'document': review.get('document'),
                'text': review.get('text'),
                'service_id': next(
                    (
                        entry.get('service_id')
                        for entry in review.get('basis', [])
                        if entry.get('service_id')
                    ),
                    None,
                ),
            }
            for review in relevant
            if review.get('state') in ('可核赔一次', '可核赔', '待分摊')
        ],
        'visits': [
            {
                'document': review['document'],
                'text': review['text'],
                'supported_visits': review['supported_visits'],
                'claimed_by': review['claimed_by'],
                'state': review['state'],
            }
            for review in relevant
        ],
        'gaps': gaps,
        'basis': _unique([
            entry for review in relevant for entry in review['basis']
        ]),
    }


def _prior_service_count(review, prior):
    if not prior:
        return None
    if isinstance(prior.get('service_visits'), int):
        return prior['service_visits']
    if isinstance(prior.get('visits'), int):
        return prior['visits']
    total = 0
    for document_id in prior.get('evidence') or []:
        for observation in review.register.service_observations(document_id):
            count = observation.visits_supported
            total += count if count is not None else 1
    return total or None


def _prior_covers_service(review, prior):
    if not prior:
        return False
    if prior.get('service_visits') or prior.get('visits'):
        return True
    items = prior.get('items') or []
    for item in items:
        if isinstance(item, str):
            if any(service['id'] == item for service in _cost_service_items(review.costs[prior['cost']])):
                return True
        elif isinstance(item, dict) and _is_service_item(item):
            return True
    return any(
        review.register.service_observations(document_id)
        for document_id in prior.get('evidence') or []
    )


def _review_parts(review, request, detail, cost_service, service_reviews, prior):
    parts = []
    for gap in detail['gaps']:
        parts.append({
            'aspect': gap['aspect'],
            'reason': gap['reason'],
            'document': gap.get('document'),
            'text': gap.get('text'),
            'doubt': gap.get('doubt'),
        })

    if not prior:
        return _unique_dicts(parts)

    covers_service = _prior_covers_service(review, prior)
    prior_count = _prior_service_count(review, prior)
    flagged = set()

    for service_review in service_reviews:
        if prior['cost'] not in service_review['claimed_by']:
            continue
        if service_review['state'] == '重复申报':
            flagged.add(tuple(service_review['key']))
            parts.append({
                'aspect': '服务次数',
                'reason': (
                    '补充材料显示同一次上门服务被多项费用引用；原决定可能按'
                    '两次服务支付，需要复核服务次数部分。'
                ),
                'document': service_review['document'],
                'text': service_review['text'],
            })
        if service_review['state'] == '待分摊':
            parts.append({
                'aspect': ALLOCATION,
                'reason': '共用一次上门服务，分摊金额未列明，原决定的服务费部分需要复核。',
                'document': service_review['document'],
                'text': service_review['text'],
            })
        if (
            covers_service
            and prior_count is not None
            and service_review['supported_visits'] < prior_count
            and tuple(service_review['key']) not in flagged
        ):
            parts.append({
                'aspect': '服务次数',
                'reason': (
                    f'原决定按 {prior_count} 次服务支付，但现有材料最多支持 '
                    f'{service_review["supported_visits"]} 次。'
                ),
                'document': service_review['document'],
                'text': service_review['text'],
            })

    if covers_service and cost_service['state'] == '待补服务单':        parts.append({
            'aspect': SERVICE,
            'reason': '原决定包含服务费，但服务单号或服务归属仍无法确认。',
        })

    # 维修范围被后补材料明确否定时，原决定的相关部分需要复核。
    for gap in detail['gaps']:
        if gap['code'] == 'denied':
            parts.append({
                'aspect': SCOPE if gap['aspect'] == SCOPE else gap['aspect'],
                'reason': gap['reason'],
                'document': gap.get('document'),
                'text': gap.get('text'),
                'denied_by': gap.get('denied_by'),
            })

    return _unique_dicts(parts)


def _unique_dicts(parts):
    seen = set()
    result = []
    for part in parts:
        key = (part.get('aspect'), part.get('reason'), part.get('document'), part.get('text'))
        if key in seen:
            continue
        seen.add(key)
        result.append(part)
    return result


def _result_basis(detail, cost_service):
    basis = []
    for aspect in DEVICE_REQUIRED_ASPECTS:
        for entry in detail['evidence'].get(aspect, []):
            basis.append({'aspect': aspect, **entry})
    for entry in cost_service.get('service_basis', []):
        basis.append({'aspect': SERVICE, **entry})
    if not cost_service.get('service_basis'):
        for entry in cost_service.get('basis', []):
            basis.append({'aspect': SERVICE, **entry})
    return _dedupe(basis)


def _assess_one(review, scope, request, service_reviews):
    detail = review.detail(request)
    cost = detail['cost']
    missing = detail['missing']
    prior = scope.decided.get(cost['id'])
    cost_service = _cost_service_view(cost['id'], service_reviews)
    review_parts = _review_parts(
        review, request, detail, cost_service, service_reviews, prior
    )

    duplicate_service = cost_service['state'] == '重复申报'
    service_gaps = list(cost_service['gaps'])
    if not service_gaps:
        service_gaps = [
            gap for gap in detail.get('service_gaps', [])
            if gap['aspect'] == SERVICE
        ]
    gaps = list(detail['gaps']) + service_gaps
    device_aspects = tuple(
        aspect for aspect in detail['evidence']
    )
    device_missing = tuple(
        aspect for aspect in missing if aspect in device_aspects
    )
    # 只有服务费的费用项没有设备证明方向，不能被当成“设备已足”。
    only_service_cost = not device_aspects
    if prior:
        if missing or review_parts:
            state = '复核原决定'
        else:
            state = '已出决定'
    elif not missing and only_service_cost:
        state = (
            '可核赔' if not service_gaps else '部分可核赔'
        )
    elif missing:
        state = '待补材料'
    elif duplicate_service or service_gaps:
        state = '部分可核赔'
    else:
        state = '可核赔'

    result = {
        'cost': cost['id'],
        'state': state,
        'missing': missing,
        'prior': prior,
        'device': detail['device'],
        'incident': detail['incident'],
        'evidence': detail['evidence'],
        'gaps': gaps,
        'service_gaps': service_gaps,
        'doubts': [],
        'service': cost_service,
        'duplicate_service': duplicate_service,
        'basis': _result_basis(detail, cost_service),
        'device_ready': not missing,
        'service_ready': not service_gaps,
        'independent': not prior and not missing and not service_gaps,
    }

    if prior and review_parts:
        result['review'] = {
            'decision': prior['id'],
            'retained': True,
            'parts': review_parts,
            'instruction': (
                '原决定保留；仅复核列明部分，复核完成前不得就同一服务重复核赔。'
            ),
        }

    if result['independent']:
        result['cost_state'] = '可核赔'
        result['split'] = [
            item.get('id') or item.get('description')
            for item in _cost_device_items(cost)
        ] or ['设备受损与维修范围']
        if cost_service['claimed']:
            result['split'].append('共用上门服务')
    elif only_service_cost:
        # 纯服务费没有独立的设备项目可先办；服务本身核赔后才可办理。
        result['cost_state'] = (
            '可核赔' if not service_gaps else '服务待定'
        )
    elif prior and not missing:
        result['cost_state'] = '已出决定' if not review_parts else '复核原决定'
    elif not missing:
        result['cost_state'] = '设备项目可先核赔，共用服务待定'
        result['split'] = [
            item.get('id') or item.get('description')
            for item in _cost_device_items(cost)
        ] or ['设备受损与维修范围']
    else:
        result['cost_state'] = '待补材料'
    result.setdefault('split', [])

    # 同一个费用项内的证明方向各自给结论：设备部分服务前已经证明的，
    # 可以先决定；共用上门服务单独判断。
    if device_aspects:
        if not cost_service['claimed']:
            service_part = '不涉及'
        else:
            service_part = (
                '待补服务单' if service_gaps else '已证明'
            )
        result['part_states'] = {
            '设备项目': '已证明' if not device_missing else '待补材料',
            '共用上门服务': service_part,
        }
    else:
        result['part_states'] = {'共用上门服务': cost_service['state']}
    return result


def assess(case, registry):
    """返回每项费用的核对结果（保持原有列表接口）。"""

    registry = _normalise_registry(registry)
    default_incident = case.get('incident')
    evidence = EvidenceRegister(
        registry['devices'], case.get('documents') or [], default_incident
    )
    review = CostReview(evidence, registry['costs'], default_incident)
    scope = SettlementScope(review, registry.get('decisions') or [])
    requests = case.get('requests') or []
    service_reviews = _service_reviews(review, requests)
    results = []
    for request in requests:
        result = _assess_one(review, scope, request, service_reviews)
        result['doubts'] = _result_doubts(evidence, result)
        results.append(result)
    return results


def _normalise_registry(registry):
    """允许在费用目录里直接放 costs/decisions 或仅放 devices。"""

    registry = dict(registry)
    registry.setdefault('costs', [])
    registry.setdefault('decisions', [])
    registry.setdefault('devices', [])
    return registry


def _result_doubts(register, result):
    doubts = []
    for document_id in register.documents:
        for doubt in register.binding(document_id).doubts:
            if doubt.device and doubt.device != result.get('device'):
                continue
            doubts.append(doubt.to_dict())
    for gap in result['gaps']:
        if gap.get('doubt'):
            doubts.append(gap['doubt'])
        elif gap.get('code') in ('denied', 'conflict', 'duplicate_service'):
            doubts.append({
                'document': gap.get('document'),
                'text': gap.get('text') or '',
                'reason': gap.get('reason'),
            })
    seen = set()
    unique = []
    for doubt in doubts:
        key = (doubt.get('document'), doubt.get('text'), doubt.get('reason'))
        if key in seen:
            continue
        seen.add(key)
        unique.append(doubt)
    return unique


def analyse_case(case, registry):
    """案件级视图：费用结果、服务核对、疑问、部件和独立费用。"""

    registry = _normalise_registry(registry)
    default_incident = case.get('incident')
    evidence = EvidenceRegister(
        registry['devices'], case.get('documents') or [], default_incident
    )
    review = CostReview(evidence, registry['costs'], default_incident)
    scope = SettlementScope(review, registry.get('decisions') or [])
    requests = case.get('requests') or []
    service_reviews = _service_reviews(review, requests)
    results = []
    for request in requests:
        result = _assess_one(review, scope, request, service_reviews)
        result['doubts'] = _result_doubts(evidence, result)
        results.append(result)

    components = {}
    for document_id in evidence.documents:
        binding = evidence.binding(document_id)
        for component in binding.components:
            entry = components.setdefault(component, {
                'component': component,
                'treated_as': '部件',
                'documents': [],
                'texts': [],
            })
            entry['documents'] = _unique(entry['documents'] + [document_id])
            entry['texts'] = _unique(entry['texts'] + [
                clause.text for clause in binding.services
                if component in clause.text
            ])
            document = evidence.documents[document_id]
            if component in (document.get('text') or ''):
                entry['texts'] = _unique(entry['texts'] + [document.get('text')])

    independent = [
        _cost_entry(result)
        for result in results
        if result['independent']
    ]
    blocked_service = [
        _cost_entry(result)
        for result in results
        if result['cost_state'] == '设备项目可先核赔，共用服务待定'
    ]
    decided = [
        _cost_entry(result)
        for result in results
        if result['prior'] and result['device_ready'] and not result.get('review')
    ]
    under_review = [
        _cost_entry(result)
        for result in results
        if result['prior'] and (not result['device_ready'] or result.get('review'))
    ]

    return {
        'incident': default_incident,
        'results': results,
        'service_visits': service_reviews,
        'doubts': _case_doubts(evidence, results),
        'components': list(components.values()),
        'independent_costs': independent,
        'device_costs_awaiting_service': blocked_service,
        'decided_costs': decided,
        'costs_under_review': under_review,
        'devices': list(evidence.devices),
        'requests_log': list(case.get('request_log') or []),
    }


def _cost_entry(result):
    return {
        'cost': result['cost'],
        'device': result['device'],
        'state': result['state'],
        'cost_state': result['cost_state'],
        'device_item_state': (
            '可核赔' if result['device_ready'] else '待补材料'
        ) if result.get('part_states', {}).get('设备项目') else '不涉及',
        'service_state': result['service']['state'],
        'basis': result['basis'],
        'basis_by_item': _basis_by_item(result),
        'pending': [
            {'aspect': gap['aspect'], 'reason': gap['reason']}
            for gap in result['gaps']
        ],
        'prior': result['prior'],
        'actionable': result['independent'],
        'review': result.get('review'),
    }


def _basis_by_item(result):
    basis = result.get('basis') or []
    groups = []
    for aspect in DEVICE_REQUIRED_ASPECTS:
        entries = [entry for entry in basis if entry.get('aspect') == aspect]
        if entries:
            groups.append({'item': aspect, 'evidence': entries})
    service_entries = [
        entry for entry in basis if entry.get('aspect') == SERVICE
    ]
    if service_entries:
        groups.append({
            'item': '共用上门服务',
            'evidence': service_entries,
            'state': (result.get('service') or {}).get('state'),
        })
    return groups


def _case_doubts(evidence, results):
    doubts = evidence.doubts()
    seen = {
        (doubt.get('document'), doubt.get('text'), doubt.get('reason'))
        for doubt in doubts
    }
    for result in results:
        for doubt in result.get('doubts', []):
            key = (doubt.get('document'), doubt.get('text'), doubt.get('reason'))
            if key in seen:
                continue
            seen.add(key)
            doubts.append(doubt)
    return doubts
