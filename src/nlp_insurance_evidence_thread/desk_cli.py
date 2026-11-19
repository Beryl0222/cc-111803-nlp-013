"""业务核对入口的命令行。

除了逐个费用的核对结果，还提供：

* ``--case``：逐项状态、缺口和疑点；
* ``--policy``：服务次数、分摊和独立费用；
* ``request`` / ``received`` / ``followup``：在同一案件内发出补件请求、
  读回后续材料是否填上缺口，并查看仍可独立办理的费用。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .desk import analyse_case, assess
from .intake import CaseStore, apply_received, build_request, followup, open_request

DEFAULT_CASE = Path('fixtures/work.json')
DEFAULT_REGISTRY = Path('fixtures/business.json')


def _load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _save_case(path, case, store):
    """把案件与原案件外的回件一起写回，供下一次查询继续使用。"""

    from .intake import _stored_documents

    known = {document.get('id') for document in case.get('documents') or []}
    extra = [
        document for document in _stored_documents(store, {'documents': []})
        if document.get('id') not in known
    ]
    if not extra:
        return
    extra_ids = [document['id'] for document in extra]
    updated = {
        **case,
        'documents': list(case.get('documents') or []) + extra,
        'requests': [
            {
                **request,
                'documents': list(request.get('documents') or []) + extra_ids,
            }
            for request in case.get('requests') or []
        ],
    }
    Path(path).write_text(
        json.dumps(updated, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
    )


def load_case(case_path, registry_path):
    """兼容：返回逐项核对结果列表。"""

    return assess(_load(case_path), _load(registry_path))


def load_analysis(case_path, registry_path):
    return analyse_case(_load(case_path), _load(registry_path))


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description='核对设备索赔材料与核赔范围')
    parser.add_argument('--case', type=Path, default=DEFAULT_CASE)
    parser.add_argument('--registry', type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument('--store', type=Path, default=None,
                        help='补件请求的保存位置（默认与案件文件同目录）')
    parser.add_argument('--view', choices=['assess', 'case', 'policy'], default='assess')

    sub = parser.add_subparsers(dest='action')

    request_parser = sub.add_parser('request', help='针对缺口发出补件请求')
    request_parser.add_argument('--cost', required=True)
    request_parser.add_argument('--device')
    request_parser.add_argument('--aspect', action='append')
    request_parser.add_argument('--documents', nargs='*')
    request_parser.add_argument('--ask')

    received_parser = sub.add_parser('received', help='登记回件')
    received_parser.add_argument('--request', required=True)
    received_parser.add_argument('--document', action='append', default=[])
    received_parser.add_argument('--case-result', type=Path)

    follow_parser = sub.add_parser('followup', help='查看仍可独立办理的费用')
    follow_parser.add_argument('--request')

    args = parser.parse_args(argv)

    try:
        if args.action:
            store = CaseStore(args.store or Path(str(args.case) + '.requests.json'))
            case = _load(args.case)
            registry = _load(args.registry)
            if args.action == 'request':
                value = build_request(
                    case, registry, args.cost, device=args.device,
                    aspects=args.aspect or [], documents=args.documents,
                    ask=args.ask,
                )
                value = open_request(store, value)
            elif args.action == 'received':
                updated_case = _load(args.case_result) if args.case_result else None
                value = apply_received(
                    store, args.request, case, registry,
                    documents=args.document,
                    updated_case=updated_case,
                )
                if args.case_result:
                    # 回件进入案件材料后才能被后续 followup 读到。
                    _save_case(args.case_result, updated_case, store)
            else:
                value = followup(case, registry, store, args.request)
        elif args.view == 'case':
            value = load_analysis(args.case, args.registry)
        elif args.view == 'policy':
            analysis = load_analysis(args.case, args.registry)
            value = {
                'service_visits': analysis['service_visits'],
                'independent_costs': analysis['independent_costs'],
                'doubts': analysis['doubts'],
            }
        else:
            value = load_case(args.case, args.registry)
    except (OSError, KeyError, ValueError) as exc:
        parser.error(str(exc))
    _print(value)


if __name__ == '__main__':
    main()
