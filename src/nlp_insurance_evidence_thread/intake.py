"""补件请求、回件登记和后续办理。

专员在同一案件里：

1. ``build_request`` 根据缺口发出具体请求（不同缺口要求不同材料）；
2. ``apply_received`` 登记回件后重新核对，指出哪些缺口确实被填上、哪些
   没有，以及新材料是否改变了服务次数或维修对象；
3. ``followup`` 列出仍可独立办理的费用及其各自依据。

原决定始终保留；新材料只产生复核提示，不覆盖原决定。
"""

from __future__ import annotations

from pathlib import Path

from .desk import (
    ALLOCATION,
    ATTRIBUTION,
    DAMAGE,
    GAP_REQUESTS,
    IDENTITY,
    SCOPE,
    SERVICE,
    analyse_case,
)
from .repository import JsonRepository

ASPECT_ORDER = (IDENTITY, DAMAGE, SCOPE, SERVICE, ATTRIBUTION, ALLOCATION)


class CaseStore:
    """补件请求的本地保存。"""

    def __init__(self, path):
        self.repository = JsonRepository(str(Path(path)))

    def all(self):
        return self.repository.read()

    def get(self, request_id):
        for item in self.all():
            if item['id'] == request_id:
                return item
        raise KeyError(f'补件请求 {request_id} 不存在')

    def save(self, request):
        items = self.all()
        for index, item in enumerate(items):
            if item['id'] == request['id']:
                items[index] = request
                break
        else:
            items.append(request)
        self.repository.replace(items)
        return request

    def next_id(self):
        existing = {item['id'] for item in self.all()}
        index = len(existing) + 1
        while f'REQ-{index}' in existing:
            index += 1
        return f'REQ-{index}'


def _stored_documents(store, case):
    """回件已进入案件材料；按材料 ID 去重，避免重复计算。"""

    known = {document.get('id') for document in case.get('documents') or []}
    result = []
    for request in store.all():
        for entry in request.get('received') or []:
            if entry.get('id') in known:
                continue
            known.add(entry.get('id'))
            result.append(entry)
    return result


def _case_with_received(case, store):
    stored = _stored_documents(store, case)
    if not stored:
        return case
    documents = list(case.get('documents') or []) + stored
    ids = [document['id'] for document in stored]
    requests = []
    for request in case.get('requests') or []:
        request = dict(request)
        request['documents'] = list(request.get('documents') or []) + ids
        requests.append(request)
    return {
        'incident': case.get('incident'),
        'documents': documents,
        'requests': requests,
    }


def _result_for(analysis, cost_id):
    for result in analysis['results']:
        if result['cost'] == cost_id:
            return result
    raise KeyError(f'费用 {cost_id} 不在当前案件中')


def _gap_aspects(result):
    aspects = []
    for gap in (result.get('gaps') or []) + (result.get('service_gaps') or []):
        if gap['aspect'] not in aspects:
            aspects.append(gap['aspect'])
    return aspects


def _gap_for(result, aspect):
    """按证明方向找缺口；服务类和设备类缺口分别来自相应部分。"""

    for gap in (result.get('gaps') or []) + (result.get('service_gaps') or []):
        if gap['aspect'] == aspect:
            return gap
    return None


def build_request(case, registry, cost_id, device=None, aspects=None, documents=None, ask=None):
    analysis = analyse_case(case, registry)
    result = _result_for(analysis, cost_id)
    available = _gap_aspects(result)
    wanted = [aspect for aspect in (aspects or ASPECT_ORDER) if aspect in available]
    if aspects and not wanted:
        raise ValueError(f'费用 {cost_id} 当前没有这些缺口：{", ".join(aspects)}')

    items = []
    for aspect in wanted:
        gap = _gap_for(result, aspect) or {}
        template = GAP_REQUESTS.get(aspect, {'documents': ['情况说明'], 'ask': f'请补充能证明{aspect}的材料。'})
        items.append({
            'aspect': aspect,
            'reason': gap.get('reason', f'未证明{aspect}。'),
            'documents': list(documents or template['documents']),
            'ask': ask or template['ask'],
            'source_document': gap.get('document'),
            'source_text': gap.get('text'),
            'doubt': gap.get('doubt'),
        })

    service_state = (result.get('service') or {}).get('state')
    if not items and service_state in ('待补服务单', '待分摊', '重复申报'):
        visits = (result.get('service') or {}).get('visits') or [{}]
        for gap in (result.get('service') or {}).get('gaps', []):
            items.append({
                'aspect': gap['aspect'],
                'reason': gap['reason'],
                'documents': list(gap['request']['documents']),
                'ask': gap['request']['ask'],
                'source_document': gap.get('document') or visits[0].get('document'),
                'source_text': gap.get('text') or visits[0].get('text'),
                'doubt': None,
            })

    if not items:
        raise ValueError(f'费用 {cost_id} 当前没有需要补件的内容')

    return {
        'cost': cost_id,
        'device': device or result.get('device'),
        'incident': result.get('incident'),
        'state_at_request': result['state'],
        'decision_at_request': (result.get('prior') or {}).get('id'),
        'decision_retained': bool(result.get('prior')),
        'items': items,
        'missing': list(result.get('missing') or []),
        'service_state': service_state,
        'status': '待回件',
        'received': [],
        'outcome': None,
    }


def open_request(store, request):
    request = dict(request)
    request['id'] = store.next_id()
    request.setdefault('received', [])
    request.setdefault('status', '待回件')
    return store.save(request)


def _new_documents(store, request_id, case, documents):
    """允许 documents 直接给结构化材料，或给已存在于案件中的材料 ID。"""

    known = {document['id']: document for document in case.get('documents') or []}
    result = []
    for value in documents:
        if isinstance(value, dict):
            result.append(value)
        elif value in known:
            result.append(known[value])
        else:
            raise ValueError(f'回件材料 {value} 未在案件中登记')
    return result


def apply_received(store, request_id, case, registry, documents=(), updated_case=None):
    request = store.get(request_id)
    if updated_case is not None:
        case = updated_case
    received = _new_documents(store, request_id, case, documents)

    # 已经并入案件材料（例如通过 --case-result 提交的更新案件）的回件
    # 不重复追加；同一张服务单可能同时影响两项费用，因此仍进入案件。
    existing = {document.get('id') for document in case.get('documents') or []}
    new_received = [
        document for document in received
        if document.get('id') not in existing
    ]
    case = _case_with_received(case, store)

    before = analyse_case(case, registry)
    before_result = _result_for(before, request['cost'])
    new_ids = [document['id'] for document in new_received]
    if not new_ids:
        after = before
    else:
        after_case = {
            'incident': case.get('incident'),
            'documents': list(case.get('documents') or []) + new_received,
            'requests': [
                {
                    **request_entry,
                    'documents': list(request_entry.get('documents') or []) + new_ids,
                }
                for request_entry in case.get('requests') or []
            ],
        }
        after = analyse_case(after_case, registry)
    after_result = _result_for(after, request['cost'])

    filled = []
    still_open = []
    unclear = []
    for item in request['items']:
        aspect = item['aspect']
        if aspect in (SERVICE, ATTRIBUTION, ALLOCATION):
            service_state = (after_result.get('service') or {}).get('state')
            if service_state in ('可核赔一次', '可核赔', '待分摊'):
                filled.append(aspect)
                continue
            still_open.append(aspect)
            gap = _gap_for(after_result, aspect)
            if gap:
                unclear.append({
                    'aspect': aspect,
                    'reason': gap.get('reason'),
                    'document': gap.get('document'),
                    'text': gap.get('text'),
                    'doubt': gap.get('doubt'),
                })
            elif aspect == SERVICE and service_state == '重复申报':
                unclear.append({
                    'aspect': aspect,
                    'reason': '新补材料仍只支持一次上门服务，多项费用不能重复核赔。',
                })
            continue

        supported = bool(after_result['evidence'].get(aspect))
        if supported:
            filled.append(aspect)
        else:
            still_open.append(aspect)
            gap = _gap_for(after_result, aspect)
            if gap:
                unclear.append({
                    'aspect': aspect,
                    'reason': gap.get('reason'),
                    'document': gap.get('document'),
                    'text': gap.get('text'),
                    'doubt': gap.get('doubt'),
                })

    service_before = (before_result.get('service') or {}).get('state')
    service_after = (after_result.get('service') or {}).get('state')
    review_parts = list((after_result.get('review') or {}).get('parts') or [])

    request = dict(request)
    request['received'] = list(request.get('received') or []) + [
        dict(document) for document in received
    ]
    request['status'] = '已回件' if filled or review_parts else '仍需补件'
    request['outcome'] = {
        'state_before': before_result['state'],
        'state_after': after_result['state'],
        'filled': filled,
        'still_open': still_open,
        'unclear': unclear,
        'decision_retained': bool(after_result.get('prior')),
        'decision': (after_result.get('prior') or {}).get('id'),
        'service_state_before': service_before,
        'service_state_after': service_after,
        'visits_supported': _visit_count(after_result),
        'review_parts': review_parts,
        'basis': after_result.get('basis'),
        'independent': after_result.get('independent'),
    }
    if review_parts:
        request['outcome']['instruction'] = '原决定保留，只复核列出的部分。'
    return store.save(request)


def _visit_count(result):
    visits = (result.get('service') or {}).get('visits') or []
    if not visits:
        return None
    supported = [visit.get('supported_visits') for visit in visits]
    supported = [value for value in supported if isinstance(value, int)]
    return max(supported) if supported else None


def followup(case, registry, store=None, request_id=None):
    if store is not None:
        case = _case_with_received(case, store)
    analysis = analyse_case(case, registry)
    result = {
        'independent_costs': analysis['independent_costs'],
        'device_costs_awaiting_service': analysis['device_costs_awaiting_service'],
        'decided_costs': analysis['decided_costs'],
        'costs_under_review': analysis['costs_under_review'],
        'still_pending': [
            {
                'cost': entry['cost'],
                'state': entry['state'],
                'missing': list(entry.get('missing') or []),
                'pending': [
                    {'aspect': gap['aspect'], 'reason': gap['reason']}
                    for gap in entry.get('gaps') or []
                ],
            }
            for entry in analysis['results']
            if not entry.get('independent')
        ],
        'service_visits': analysis['service_visits'],
        'doubts': analysis['doubts'],
        'decisions': [
            {
                'cost': entry['cost'],
                'decision': (entry.get('prior') or {}).get('id'),
                'retained': bool(entry.get('prior')),
                'review': entry.get('review'),
            }
            for entry in analysis['results']
            if entry.get('prior')
        ],
    }
    if request_id and store is not None:
        request = store.get(request_id)
        result['request'] = {
            'id': request['id'],
            'cost': request['cost'],
            'status': request['status'],
            'items': request['items'],
            'received': request['received'],
            'outcome': request.get('outcome'),
        }
    return result
