#!/usr/bin/env python3
import argparse, json
from copy import deepcopy
from datetime import datetime, timezone


def load(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def save(data, path):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')

def build_structural_indexes(structural):
    b = structural.get('bim', {})
    interfaces = b.get('behaviorInterfaces', [])
    iface_by_name = {i.get('name'): i for i in interfaces}
    op_by_guid = {}
    op_by_ref = {}
    for iface in interfaces:
        iname = iface.get('name')
        role = iface.get('role') or iface.get('stereotype')
        for op in iface.get('operations', []) or []:
            ref = f"{iname}.{op.get('name')}" if iname and op.get('name') else None
            item = dict(op)
            item['interface'] = iname
            item['role'] = role
            item['operationRef'] = ref
            if op.get('id'):
                op_by_guid[op.get('id')] = item
            if ref:
                op_by_ref[ref] = item
    known_types = set(structural.get('typeSummary', {}).get('primitiveTypes', []))
    known_types.update(['void'])
    known_types.update(d.get('name') for d in structural.get('api', {}).get('dtos', []) if d.get('name'))
    known_types.update(e.get('name') for e in structural.get('api', {}).get('enums', []) if e.get('name'))
    known_types.update(e.get('name') for e in structural.get('persistence', {}).get('entities', []) if e.get('name'))
    known_types.update(r.get('name') for r in b.get('responses', []) if r.get('name'))
    known_types.update(p.get('name') for p in b.get('parameterSets', []) if p.get('name'))
    # Include abstract HttpResponse if present as response base
    for r in b.get('responses', []):
        if r.get('baseType'):
            known_types.add(r.get('baseType'))
    endpoint_ops = {e.get('name'): e for e in b.get('endpointOperations', []) if e.get('name')}
    return iface_by_name, op_by_guid, op_by_ref, known_types, endpoint_ops

def enrich_bfm(bfm, structural):
    bfm = deepcopy(bfm)
    iface_by_name, op_by_guid, op_by_ref, known_types, endpoint_ops = build_structural_indexes(structural)

    for behavior in bfm.get('bm', {}).get('behaviors', []) or []:
        # Lifeline roles
        for lifeline in behavior.get('lifelines', []) or []:
            rep = lifeline.get('represents') or lifeline.get('name')
            iface = iface_by_name.get(rep)
            if iface:
                lifeline['role'] = iface.get('role') or iface.get('stereotype')

        # Message operation refs and operation details
        for msg in behavior.get('messages', []) or []:
            guid = msg.get('operationGuid')
            op = op_by_guid.get(guid)
            if op:
                msg['operationRef'] = op.get('operationRef')
                msg['declaredReturnType'] = msg.get('declaredReturnType') or op.get('returnType')
                if msg.get('kind') in ('sync', 'self') and not msg.get('returnType'):
                    msg['returnType'] = op.get('returnType')
            # normalize forward return: no return type
            if msg.get('kind') == 'return' and msg.get('returnMode') == 'forward':
                msg['returnType'] = None

    # Rebuild diagnostics with structural-level validation, but keep non-type warnings that matter.
    diagnostics = []
    for d in bfm.get('diagnostics', []) or []:
        code = d.get('code')
        if code in {'UNKNOWN_RETURN_TYPE', 'REPRESENTS_INFERRED_BY_NAME'}:
            continue
        diagnostics.append(d)

    # Add validations
    for behavior in bfm.get('bm', {}).get('behaviors', []) or []:
        rep = behavior.get('represents')
        if rep and rep not in endpoint_ops:
            diagnostics.append({
                'level': 'error',
                'code': 'UNKNOWN_BEHAVIOR_REPRESENTS',
                'message': f'Behavior {behavior.get("name")} represents {rep}, but this endpoint operation is not present in structural IR.'
            })
        for lifeline in behavior.get('lifelines', []) or []:
            rep_l = lifeline.get('represents') or lifeline.get('name')
            if rep_l not in iface_by_name:
                diagnostics.append({
                    'level': 'warning',
                    'code': 'UNKNOWN_LIFELINE_INTERFACE',
                    'message': f'Lifeline {lifeline.get("name")} represents {rep_l}, but this interface is not present in structural IR.'
                })
        for msg in behavior.get('messages', []) or []:
            if msg.get('kind') in ('sync','self') and msg.get('operation') and not msg.get('operationRef'):
                diagnostics.append({
                    'level': 'warning',
                    'code': 'UNRESOLVED_OPERATION_REF',
                    'message': f'Message {msg.get("seq")} invokes {msg.get("operation")}, but operationRef was not resolved from structural IR.'
                })
            for key in ('returnType','declaredReturnType'):
                t = msg.get(key)
                if t and t not in known_types:
                    diagnostics.append({
                        'level': 'warning',
                        'code': 'UNKNOWN_TYPE',
                        'message': f'Message {msg.get("seq")} has {key} {t}, which was not found in structural IR.'
                    })

    bfm['metadata']['structuralValidation'] = True
    bfm['diagnostics'] = diagnostics
    return bfm

def merge(structural, bfm):
    enriched_bfm = enrich_bfm(bfm, structural)
    combined_diag = []
    combined_diag.extend(structural.get('diagnostics', []) or [])
    combined_diag.extend(enriched_bfm.get('diagnostics', []) or [])
    result = {
        'metadata': {
            'stage': 'rest',
            'description': 'Combined REST IR: structural API/Persistence/BIM IR enriched with BM behavior IR.',
            'createdAt': datetime.now(timezone.utc).isoformat(),
            'sources': {
                'structural': structural.get('metadata', {}).get('sources', {}),
                'behavior': enriched_bfm.get('metadata', {}).get('source')
            }
        },
        'api': structural.get('api', {}),
        'persistence': structural.get('persistence', {}),
        'mappings': structural.get('mappings', {}),
        'typeSummary': structural.get('typeSummary', {}),
        'bim': structural.get('bim', {}),
        'bm': enriched_bfm.get('bm', {}),
        'diagnostics': combined_diag
    }
    return result

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--structural', required=True)
    ap.add_argument('--bm', required=True)
    ap.add_argument('--out', required=True)
    args = ap.parse_args()
    rest = merge(load(args.structural), load(args.bm))
    save(rest, args.out)
    print(f'ReST IR written to {args.out}')
    print(f"DTOs: {len(rest.get('api',{}).get('dtos',[]))}")
    print(f"Endpoint operations: {len(rest.get('bim',{}).get('endpointOperations',[]))}")
    print(f"Behaviors: {len(rest.get('bm',{}).get('behaviors',[]))}")
    print(f"Diagnostics: {len(rest.get('diagnostics',[]))}")

if __name__ == '__main__':
    main()
