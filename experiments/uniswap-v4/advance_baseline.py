#!/usr/bin/env python3
"""Apply only a verified, closed repair to the next benchmark baseline."""
import argparse
import json
from pathlib import Path

import loop
from execution import digest, patch_contract


def next_version(report, baseline, spec, source):
    if report.get('status') != 'no_demonstrated_mismatch':
        return None
    rounds = report.get('rounds', [])
    if len(rounds) != 2 or report.get('baseline_revision') != baseline['revision']:
        raise ValueError('Repair did not start from the current baseline')
    first, second = rounds
    intent_hash = digest(json.loads((loop.run.BASE / 'intent.json').read_text()))
    if report.get('intent_sha256') != intent_hash:
        raise ValueError('Creator intent differs from the verified report')
    version = digest(dict(spec=spec, contract=baseline['contract'], source=source,
                          intent_sha256=intent_hash))
    if (first.get('version') != version or first.get('spec') != spec or
            first.get('contract') != baseline['contract']):
        raise ValueError('Repair inputs differ from the current baseline')
    if (first.get('verification', {}).get('kernel_checked') is not True or
            first.get('repair_recheck', {}).get('kernel_checked') is not True or
            first.get('repair_recheck', {}).get('comparison', {}).get('findings') or
            first.get('status') != 'repair_replayed_pending_review' or
            second.get('verification', {}).get('kernel_checked') is not True or
            second.get('comparison', {}).get('findings') or
            second.get('judgment', {}).get('verdict') != 'unsupported'):
        raise ValueError('Repair replay or independent second review did not pass')
    judgment = loop.validate_judgment(first['judgment'], first['comparison'])
    proposal = loop.validate_repair(first['repair'], judgment, spec, baseline['contract'])
    updated = {**baseline, 'revision': baseline['revision'] + 1}
    if proposal['target'] == 'contract':
        changed_spec, changed_source, changed_contract = spec, patch_contract(source), 'patched'
        updated.update(contract='patched', contract_source='specs/active/RebateHook.sol')
    else:
        changed = {**spec, 'claim_limit': 'cumulative'} if proposal['change'] == 'cumulative' else {
            **spec, 'minimum_rebate': 0}
        changed_spec, changed_source, changed_contract = loop.validate_spec(changed), None, baseline['contract']
    expected_version = digest(dict(spec=changed_spec, contract=changed_contract,
                                   source=changed_source or source, intent_sha256=intent_hash))
    if (second.get('parent_version') != first['version'] or
            second.get('version') != expected_version or second.get('spec') != changed_spec or
            second.get('contract') != changed_contract):
        raise ValueError('Second review did not verify the exact proposed baseline')
    return updated, changed_spec, changed_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--base', type=Path, required=True, help='Checked-out current benchmark branch')
    args = parser.parse_args()
    root = args.base.resolve() / 'experiments/uniswap-v4'
    baseline = json.loads((root / 'baseline.json').read_text())
    spec = loop.validate_spec(json.loads((root / 'specs/base.json').read_text()))
    source = (root / baseline['contract_source']).read_text()
    report = json.loads(args.report.read_text())
    result = next_version(report, baseline, spec, source)
    if result is None:
        print('No verified repair to advance')
        return
    updated, changed_spec, changed_source = result
    (root / 'baseline.json').write_text(json.dumps(updated, indent=2) + '\n')
    (root / 'specs/base.json').write_text(json.dumps(changed_spec, indent=2) + '\n')
    if changed_source is not None:
        destination = root / updated['contract_source']
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(changed_source)
    print('Advanced baseline to revision', updated['revision'])


if __name__ == '__main__':
    main()
