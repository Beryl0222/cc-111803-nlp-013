class EvidenceRegister:
    def __init__(self, devices, documents):
        self.devices = {item['id']: item['site'] for item in devices}
        self.documents = {item['id']: item for item in documents}

    def supports(self, document_id, device, incident):
        item = self.documents[document_id]
        if item['site'] != self.devices[device] or item['device'] != device or item['incident'] != incident:
            return None
        return item['kind']


class CostReview:
    def __init__(self, register, costs):
        self.register = register
        self.costs = {cost['id']: cost for cost in costs}

    def classify(self, request):
        cost = self.costs[request['cost']]
        supported = {self.register.supports(doc, cost['device'], request['incident']) for doc in request['documents']}
        missing = {'设备受损', '维修范围'} - supported
        return cost, sorted(missing)


class SettlementScope:
    def __init__(self, review, decided):
        self.review = review
        self.decided = {decision['cost']: decision for decision in decided}

    def eligible(self, request):
        cost, missing = self.review.classify(request)
        prior = self.decided.get(cost['id'])
        if prior:
            state = '已出决定' if not missing else '复核原决定'
        else:
            state = '待补材料' if missing else '可核赔'
        return {'cost': cost['id'], 'state': state, 'missing': missing, 'prior': prior}


def assess(case, registry):
    evidence = EvidenceRegister(registry['devices'], case['documents'])
    review = CostReview(evidence, registry['costs'])
    scope = SettlementScope(review, registry['decisions'])
    return [scope.eligible(request) for request in case['requests']]
