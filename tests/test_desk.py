from pathlib import Path

from nlp_insurance_evidence_thread.desk_cli import load_analysis, load_case
from nlp_insurance_evidence_thread.extraction import extract_entities
from nlp_insurance_evidence_thread.intake import (
    CaseStore,
    apply_received,
    build_request,
    followup,
    open_request,
)


FIXTURE = Path(__file__).parents[1] / 'fixtures'


def _case(name='case-shared-service.json'):
    import json
    return json.loads((FIXTURE / name).read_text(encoding='utf-8'))


def _registry():
    import json
    return json.loads((FIXTURE / 'business.json').read_text(encoding='utf-8'))


def test_other_device_in_same_site_cannot_supply_repair_evidence():
    output = load_case(FIXTURE / 'work.json', FIXTURE / 'business.json')
    assert output[0]['state'] == '已出决定'
    assert output[0]['prior']['id'] == 'DEC-8'
    assert output[1]['state'] == '待补材料'
    assert output[1]['missing'] == ['维修范围']


def test_detached_shell_is_a_component_not_a_second_device():
    extraction = extract_entities('主机进水，拆下外壳送修。')
    assert extraction.devices == ()
    assert '外壳' in extraction.components

    analysis = load_analysis(FIXTURE / 'case-shared-service.json', FIXTURE / 'business.json')
    components = {item['component']: item for item in analysis['components']}
    assert components['外壳']['treated_as'] == '部件'
    reasons = [doubt['reason'] for doubt in analysis['doubts']]
    assert any('部件' in reason for reason in reasons)
    assert '外壳' not in analysis['devices']


def test_repair_scope_of_one_device_cannot_be_copied_to_another():
    analysis = load_analysis(FIXTURE / 'case-shared-service.json', FIXTURE / 'business.json')
    result = {item['cost']: item for item in analysis['results']}['COST-2']
    assert '维修范围' in result['missing']
    scope_gap = next(gap for gap in result['gaps'] if gap['aspect'] == '维修范围')
    assert scope_gap['document'] == 'repair-1'
    assert scope_gap['text']
    assert scope_gap['devices'] == ['Z91']


def test_shared_single_visit_cannot_be_claimed_twice():
    analysis = load_analysis(FIXTURE / 'case-shared-service.json', FIXTURE / 'business.json')
    visit = analysis['service_visits'][0]
    assert visit['supported_visits'] == 1
    assert visit['claimed_visits'] == 2
    assert visit['state'] == '重复申报'
    for result in analysis['results']:
        assert result['service']['state'] == '重复申报'
        assert result['independent'] is False


def test_prior_decision_is_retained_and_points_to_service_part():
    analysis = load_analysis(FIXTURE / 'case-shared-service.json', FIXTURE / 'business.json')
    result = {item['cost']: item for item in analysis['results']}['COST-1']
    assert result['state'] == '复核原决定'
    assert result['review']['retained'] is True
    assert result['review']['decision'] == 'DEC-8'
    aspects = {part['aspect'] for part in result['review']['parts']}
    assert '服务次数' in aspects
    assert result['missing'] == []


def test_missing_scope_and_missing_identity_need_different_requests():
    case = _case()
    registry = _registry()
    # 第二项没有任何材料时，设备身份和维修范围都未证明，但补件要求不同。
    for request in case['requests']:
        if request['cost'] == 'COST-2':
            request['documents'] = []
    analysis = load_analysis_rows(case, registry)
    z19 = {item['cost']: item for item in analysis}['COST-2']
    assert z19['missing'] == ['设备身份', '设备受损', '维修范围']
    scope_gap = next(gap for gap in z19['gaps'] if gap['aspect'] == '维修范围')
    identity_gap = next(gap for gap in z19['gaps'] if gap['aspect'] == '设备身份')
    assert scope_gap['request']['documents'] != identity_gap['request']['documents']
    assert '序列号' in identity_gap['request']['ask']
    assert '维修项目' in scope_gap['request']['ask']
    assert scope_gap['request']['ask'] != identity_gap['request']['ask']


def load_analysis_rows(case, registry):
    from nlp_insurance_evidence_thread.desk import assess
    return assess(case, registry)


def test_request_followup_fills_gap_with_service_detail(tmp_path):
    case = _case()
    registry = _registry()
    store = CaseStore(tmp_path / 'requests.json')

    request = open_request(store, build_request(case, registry, 'COST-1'))
    aspects = {item['aspect'] for item in request['items']}
    assert aspects == {'服务依据'}
    assert request['decision_retained'] is True

    service_sheet = {
        'id': 'service-1',
        'kind': '上门服务单',
        'service_id': 'SV-77',
        'incident': 'CL-9',
        'site': '仓库东区',
        'text': '服务单 SV-77：2026-11-02 上门一次，处理 Z91 与 Z19，上门费只收一次，两台各分摊 4,000 元。',
    }
    updated = apply_received(store, request['id'], case, registry, documents=[service_sheet])
    outcome = updated['outcome']
    assert '服务依据' in outcome['filled']
    assert outcome['still_open'] == []
    assert outcome['service_state_after'] == '可核赔一次'
    assert outcome['decision_retained'] is True
    # 新服务单证明只有一次上门，原决定按两次支付的部分需要复核。
    reasons = ' '.join(part['reason'] for part in outcome['review_parts'])
    assert '1 次' in reasons and '2 次' in reasons

    after = followup(case, registry, store)
    under_review = {entry['cost'] for entry in after['costs_under_review']}
    assert 'COST-1' in under_review
    assert after['independent_costs'] == []


def test_reply_showing_one_device_was_not_served_narrows_service(tmp_path):
    case = _case()
    registry = _registry()
    store = CaseStore(tmp_path / 'requests.json')

    request = open_request(store, build_request(case, registry, 'COST-1'))
    sheet = {
        'id': 'service-1',
        'kind': '上门服务单',
        'service_id': 'SV-90',
        'incident': 'CL-9',
        'site': '仓库东区',
        'text': '服务单 SV-90：2026-11-02 上门服务一次，仅处理 Z91，Z19 未上门。',
    }
    updated = apply_received(store, request['id'], case, registry, documents=[sheet])
    outcome = updated['outcome']
    assert '服务依据' in outcome['filled']
    assert outcome['visits_supported'] == 1
    reasons = ' '.join(part['reason'] for part in outcome['review_parts'])
    assert '2 次' in reasons and '1 次' in reasons
    assert outcome['decision_retained'] is True


def test_reply_showing_two_separate_visits_keeps_both(tmp_path):
    case = _case()
    registry = _registry()
    store = CaseStore(tmp_path / 'requests.json')

    request = open_request(store, build_request(case, registry, 'COST-1'))
    sheet = {
        'id': 'service-1',
        'kind': '上门服务单',
        'service_id': 'SV-91',
        'incident': 'CL-9',
        'site': '仓库东区',
        'text': '服务单 SV-91：2026-11-02 为 Z91 与 Z19 各上门服务一次，合计两次。',
    }
    updated = apply_received(store, request['id'], case, registry, documents=[sheet])
    outcome = updated['outcome']
    assert '服务依据' in outcome['filled']
    assert outcome['visits_supported'] == 2
    assert outcome['review_parts'] == []
    assert outcome['decision_retained'] is True
    # 两次独立上门且分摊清楚后，原决定不再需要就服务次数复核。
    assert updated['status'] == '已回件'


def test_identity_gap_asks_for_serial_number_not_repair_sheet(tmp_path):
    case = _case()
    registry = _registry()
    case['requests'][1]['documents'] = []
    store = CaseStore(tmp_path / 'requests.json')

    request = open_request(store, build_request(case, registry, 'COST-2', aspects=['设备身份']))
    item = request['items'][0]
    assert item['aspect'] == '设备身份'
    assert item['documents'] == ['铭牌照片', '资产台账或设备清单']
    assert '序列号' in item['ask']

    plate = {
        'id': 'plate-2',
        'kind': '铭牌照片',
        'device': 'Z19',
        'incident': 'CL-9',
        'site': '仓库东区',
        'text': '照片：Z19 铭牌，序列号 Z19。',
    }
    updated = apply_received(store, request['id'], case, registry, documents=[plate])
    assert updated['outcome']['filled'] == ['设备身份']
    # 铭牌只补身份，不会假装补上设备受损或维修范围。
    pending = {entry['cost']: entry for entry in followup(case, registry, store)['still_pending']}
    assert '设备受损' in pending['COST-2']['missing']
    assert '维修范围' in pending['COST-2']['missing']


def test_reply_that_does_not_fill_gap_stays_open(tmp_path):
    case = _case()
    registry = _registry()
    store = CaseStore(tmp_path / 'requests.json')

    request = open_request(store, build_request(case, registry, 'COST-2'))
    aspects = {item['aspect'] for item in request['items']}
    assert aspects == {'设备受损', '维修范围', '服务依据'}

    unrelated = {
        'id': 'inspection-1',
        'kind': '查勘记录',
        'incident': 'CL-9',
        'site': '仓库东区',
        'text': '两台同型主机上门检查一次，未发现外壳损坏。',
    }
    updated = apply_received(store, request['id'], case, registry, documents=[unrelated])
    outcome = updated['outcome']
    assert outcome['filled'] == []
    assert set(outcome['still_open']) == {'设备受损', '维修范围', '服务依据'}
    assert updated['status'] == '仍需补件'
    assert outcome['unclear']


def test_followup_lists_remaining_independent_costs_with_basis(tmp_path):
    case = _case()
    registry = _registry()
    store = CaseStore(tmp_path / 'requests.json')
    followup_result = followup(case, registry, store)
    # 两项都有共用服务问题，因此没有可直接办理的费用。
    assert followup_result['independent_costs'] == []

    # 只保留 Z91 一项，并补一张明确服务单：设备项目可独立办理。
    case = _case()
    case['requests'] = [request for request in case['requests'] if request['cost'] == 'COST-1']
    service_sheet = {
        'id': 'service-1',
        'kind': '上门服务单',
        'service_id': 'SV-77',
        'incident': 'CL-9',
        'site': '仓库东区',
        'text': '服务单 SV-77：2026-11-02 上门一次，处理 Z91，上门费 8,000 元。',
    }
    registry = _registry()
    registry['decisions'] = []
    case = {
        **case,
        'documents': list(case['documents']) + [service_sheet],
        'requests': [
            {
                **case['requests'][0],
                'documents': list(case['requests'][0]['documents']) + ['service-1'],
            }
        ],
    }

    result = followup(case, registry, store)
    independent = {entry['cost']: entry for entry in result['independent_costs']}
    assert 'COST-1' in independent
    basis = independent['COST-1']['basis_by_item']
    assert {group['item'] for group in basis} >= {'维修范围', '共用上门服务'}
    service_group = next(group for group in basis if group['item'] == '共用上门服务')
    assert service_group['evidence'][0]['document'] == 'service-1'


def test_doubt_points_to_source_text_that_caused_it():
    analysis = load_analysis(FIXTURE / 'case-shared-service.json', FIXTURE / 'business.json')
    z19 = {item['cost']: item for item in analysis['results']}['COST-2']
    damage_gap = next(gap for gap in z19['gaps'] if gap['aspect'] == '设备受损')
    assert damage_gap['code'] == 'denied'
    assert damage_gap['document'] == 'photo-2'
    assert '未见进水' in damage_gap['text']
