from pathlib import Path

from nlp_insurance_evidence_thread.desk_cli import load_case


def test_other_device_in_same_site_cannot_supply_repair_evidence():
    fixture = Path(__file__).parents[1] / 'fixtures'
    output = load_case(fixture / 'work.json', fixture / 'business.json')
    assert output[0]['state'] == '已出决定'
    assert output[0]['prior']['id'] == 'DEC-8'
    assert output[1]['state'] == '待补材料'
    assert output[1]['missing'] == ['维修范围']
