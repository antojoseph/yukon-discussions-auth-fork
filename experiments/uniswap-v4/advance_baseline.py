#!/usr/bin/env python3
"""Apply only a verified, closed repair to the next benchmark baseline."""
import argparse
import json
from pathlib import Path
from datetime import datetime, timezone

import loop
from execution import compare, digest, execute, patch_contract


def checked_traces(root, report_path, history):
    """Use the recorded trace, not a fresh challenger, for every promotion replay."""
    if not isinstance(history, dict) or history.get('schema_version') != 1 or not isinstance(history.get('traces'), list):
        raise ValueError('Invalid falsification history')
    traces = []
    for entry in history['traces']:
        if not isinstance(entry, dict) or set(entry) != {'sha256', 'trace'}:
            raise ValueError('Invalid historical trace entry')
        relative = Path(entry['trace'])
        if relative.is_absolute() or '..' in relative.parts or relative.parts[:1] != ('fixtures',):
            raise ValueError('Historical trace path must be under fixtures')
        trace = loop.run.read_submission(root / relative)
        if digest(trace) != entry['sha256']:
            raise ValueError('Historical trace digest changed')
        traces.append((entry['trace'], trace))
    current_path = report_path.parent / 'round-1/trace.json'
    current = loop.run.read_submission(current_path)
    traces.append(('new-submission', current))
    return traces


def verify_candidate(root, report_path, history, spec, source, contract):
    """Re-execute all recorded falsifications on the exact proposed artifacts."""
    traces = checked_traces(root, report_path, history)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    evidence_root = loop.run.ROOT / 'runs' / ('reproduced-baseline-promotion-' + stamp)
    evidence_root.mkdir(parents=True, exist_ok=False)
    results = []
    for index, (name, trace) in enumerate(traces):
        folder = evidence_root / str(index)
        observed = execute(trace, contract == 'patched', source, folder / 'execution', patched=contract == 'patched')
        comparison = compare(spec, observed)
        if comparison['findings']:
            raise ValueError('Proposed baseline still falsified by ' + name)
        kernel_checked = False
        if not any(action['op'] == 'call' for action in trace['actions']):
            trace_path = folder / 'trace.json'
            loop.write(trace_path, trace)
            proof = loop.proof(trace_path, loop.run.ROOT / 'runs' / ('reproduced-baseline-proof-' + stamp + '-' + str(index)))
            if proof.get('kernel_checked') is not True or proof.get('concrete_abstract_replay_agree') is not True:
                raise ValueError('Promotion proof failed for ' + name)
            kernel_checked = True
        results.append(dict(trace=name, sha256=digest(trace), kernel_checked=kernel_checked,
                            evm_executed=True, regressions_passed=observed['regressions_passed']))
    loop.write(evidence_root / 'promotion-checks.json', dict(schema_version=1, results=results))
    return traces[-1][1]


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
    has_raw_call = any(tx.get('action', {}).get('op') == 'call'
                       for tx in first.get('execution', {}).get('transactions', []))
    first_checked = (first.get('verification', {}).get('evm_executed') is True if has_raw_call else
                     first.get('verification', {}).get('kernel_checked') is True)
    second_checked = (second.get('verification', {}).get('evm_executed') is True if has_raw_call else
                      second.get('verification', {}).get('kernel_checked') is True)
    repair_checked = (first.get('repair_recheck', {}).get('execution', {}).get('execution') == 'local_foundry_evm_calls'
                      if has_raw_call else first.get('repair_recheck', {}).get('kernel_checked') is True)
    if (not first_checked or not repair_checked or
            first.get('repair_recheck', {}).get('comparison', {}).get('findings') or
            first.get('status') != 'repair_replayed_pending_review' or
            not second_checked or
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
    history_path = root / 'specs/falsification-history.json'
    history = json.loads(history_path.read_text())
    candidate_source = changed_source if changed_source is not None else source
    current_trace = verify_candidate(root, args.report.resolve(), history, changed_spec,
                                     candidate_source, updated['contract'])
    current_hash = digest(current_trace)
    if current_hash not in {entry['sha256'] for entry in history['traces']}:
        trace_name = 'fixtures/verified-' + current_hash[:16] + '.json'
        trace_path = root / trace_name
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        loop.write(trace_path, current_trace)
        history['traces'].append(dict(sha256=current_hash, trace=trace_name))
        loop.write(history_path, history)
    (root / 'baseline.json').write_text(json.dumps(updated, indent=2) + '\n')
    (root / 'specs/base.json').write_text(json.dumps(changed_spec, indent=2) + '\n')
    if changed_source is not None:
        destination = root / updated['contract_source']
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(changed_source)
    print('Advanced baseline to revision', updated['revision'])


if __name__ == '__main__':
    main()
