#!/usr/bin/env python3
"""Record a credited trace and promote a repair only after exact-candidate checks."""
import argparse
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone

import loop
import extension
from execution import compare, digest, execute, patch_contract


def checked_traces(root, report_path, history, current_is_trace=False):
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
    current_path = report_path if current_is_trace else report_path.parent / 'round-1/trace.json'
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


def next_extension_version(report, baseline, spec, source, lean_source, history, trace_path, root):
    if report.get('status') != 'extension_verified':
        return None
    if (report.get('accepted') is not False or report.get('creator_approval') != 'pending' or
            report.get('contract_correspondence') != 'not_proved' or
            report.get('judge_mode') != 'hosted_openrouter_tool_free_roles'):
        raise ValueError('Extension report lacks hosted review provenance')
    rounds = report.get('rounds', [])
    if len(rounds) != 1 or report.get('baseline_revision') != baseline['revision']:
        raise ValueError('Extension did not start from the current baseline')
    if baseline['contract'] != 'patched':
        raise ValueError('Extension replay requires the active patched fixture')
    first = rounds[0]
    intent_hash = digest(json.loads((root / 'intent.json').read_text()))
    version_inputs = dict(spec=spec, contract=baseline['contract'], source=source,
                          intent_sha256=intent_hash)
    if baseline.get('lean_source'):
        version_inputs['lean_source'] = lean_source
    if (report.get('intent_sha256') != intent_hash or first.get('status') != 'extension_verified' or
            first.get('version') != digest(version_inputs) or
            first.get('spec') != spec or first.get('contract') != baseline['contract'] or
            first.get('verification', {}).get('evm_executed') is not True or
            first.get('execution', {}).get('source_sha256') != hashlib.sha256(source.encode()).hexdigest()):
        raise ValueError('Extension report differs from the current baseline')
    transaction_ids = [tx['id'] for tx in first['execution']['transactions']]
    judgment = loop.validate_judgment(first['judgment'], first['comparison'], transaction_ids)
    if judgment['verdict'] not in ('contract_defect', 'spec_defect', 'both'):
        raise ValueError('Extension lacks a supported initial falsification')
    candidate_spec, candidate_source, candidate_lean = extension.parse_proposal(
        first['spec_extension_proposal'], spec, source, lean_source,
        judgment['verdict'], judgment.get('spec_gap', False))
    recorded = first['extension_recheck']
    candidate_hash = digest({'spec': candidate_spec, 'contract_source': candidate_source,
                             'lean_source': candidate_lean})
    if recorded.get('candidate_sha256') != candidate_hash or recorded.get('formal', {}).get('model_kernel_checked') is not True:
        raise ValueError('Extension check does not bind the proposed files')
    traces = checked_traces(root, trace_path, history, current_is_trace=True)
    if [case.get('trace_sha256') for case in recorded.get('cases', [])] != [digest(trace) for _, trace in traces]:
        raise ValueError('Extension review did not cover the full falsification history')
    reviews = first.get('extension_reviews')
    if not isinstance(reviews, list) or len(reviews) != len(recorded['cases']):
        raise ValueError('Independent reviews do not cover every trace')
    for review, case in zip(reviews, recorded['cases']):
        checked = extension.validate_review(review, {**recorded, 'cases': [case]})
        if checked['verdict'] != 'resolved':
            raise ValueError('Independent review did not resolve a cited trace')
    updated = {**baseline, 'revision': baseline['revision'] + 1, 'contract': 'patched',
               'contract_source': 'specs/active/RebateHook.sol',
               'lean_source': 'specs/active/Rebate.lean'}
    return updated, candidate_spec, candidate_source, candidate_lean


def verified_credit(score, report, trace):
    import benchmark
    metrics = score.get('metrics', {})
    finding = benchmark.supported_falsification(report, trace['requirement'])
    if (score.get('score') != 1 or metrics.get('verified_falsification') != 1 or
            finding != (metrics.get('repair_target'), metrics.get('defect_key'))):
        raise ValueError('Score does not match the judged falsification')
    return metrics.get('host_repair_verified') is True


def promote_candidate(root, report_path, report, baseline, spec, source, history, trace_path):
    if report.get('status') == 'extension_verified':
        active_lean = (root / baseline.get('lean_source', 'lean/Rebate.lean')).read_text()
        updated, changed_spec, changed_source, changed_lean = next_extension_version(
            report, baseline, spec, source, active_lean, history, trace_path, root)
        replay = extension.replay_candidate(root, trace_path, history, changed_spec,
                                            changed_source, changed_lean,
                                            loop.run.ROOT / 'runs' / ('reproduced-extension-promotion-' +
                                                datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')))
        recorded = report['rounds'][0]['extension_recheck']
        if (replay['candidate_sha256'] != recorded['candidate_sha256'] or
                [case['trace_sha256'] for case in replay['cases']] !=
                [case['trace_sha256'] for case in recorded['cases']] or
                [digest(case['execution']) for case in replay['cases']] !=
                [digest(case['execution']) for case in recorded['cases']]):
            raise ValueError('Promotion replay differs from the reviewed candidate')
        return updated, changed_spec, changed_source, changed_lean
    result = next_version(report, baseline, spec, source)
    if result is None:
        raise ValueError('Report contains no verified repair')
    updated, changed_spec, changed_source = result
    verify_candidate(root, report_path, history, changed_spec,
                     changed_source if changed_source is not None else source,
                     updated['contract'])
    return updated, changed_spec, changed_source, None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--base', type=Path, required=True, help='Checked-out current benchmark branch')
    parser.add_argument('--score', type=Path, help='Authoritative score for recording a credited trace')
    parser.add_argument('--result', type=Path, help='Write promotion outcome JSON')
    args = parser.parse_args()
    root = args.base.resolve() / 'experiments/uniswap-v4'
    baseline = json.loads((root / 'baseline.json').read_text())
    spec = loop.validate_spec(json.loads((root / 'specs/base.json').read_text()))
    source = (root / baseline['contract_source']).read_text()
    report = json.loads(args.report.read_text())
    history_path = root / 'specs/falsification-history.json'
    history = json.loads(history_path.read_text())
    trace_path = args.report.resolve().parent / 'round-1/trace.json'
    current_trace = loop.run.read_submission(trace_path)
    score = json.loads(args.score.read_text()) if args.score else None
    should_promote = verified_credit(score, report, current_trace) if score else report.get('status') in (
        'no_demonstrated_mismatch', 'extension_verified')
    promotion = None
    promotion_error = None
    if should_promote:
        try:
            promotion = promote_candidate(root, args.report.resolve(), report, baseline,
                                          spec, source, history, trace_path)
        except Exception as error:
            if score is None:
                raise
            promotion_error = str(error)[:2000]
    elif score is None:
        print('No verified repair to advance')
        return
    current_hash = digest(current_trace)
    recorded = False
    if current_hash not in {entry['sha256'] for entry in history['traces']}:
        trace_name = 'fixtures/verified-' + current_hash[:16] + '.json'
        trace_path = root / trace_name
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        loop.write(trace_path, current_trace)
        history['traces'].append(dict(sha256=current_hash, trace=trace_name))
        loop.write(history_path, history)
        recorded = True
    if promotion is not None:
        updated, changed_spec, changed_source, changed_lean = promotion
        (root / 'baseline.json').write_text(json.dumps(updated, indent=2) + '\n')
        (root / 'specs/base.json').write_text(json.dumps(changed_spec, indent=2) + '\n')
        if changed_source is not None:
            destination = root / updated['contract_source']
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(changed_source)
        if changed_lean is not None:
            destination = root / updated['lean_source']
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(changed_lean)
    progress = {'schema_version': 1,
                'state': 'active' if promotion else 'stalled',
                'baseline_revision': promotion[0]['revision'] if promotion else baseline['revision'],
                'finding_sha256': None if promotion else current_hash,
                'reason': None if promotion else
                          'A verified finding was recorded, but the host did not promote a checked repair. Manual baseline repair and review are required.'}
    if score is not None:
        loop.write(root / 'specs/progress.json', progress)
    result = {'status': 'baseline_updated' if promotion else 'finding_recorded' if recorded else 'no_change',
              'baseline_revision': promotion[0]['revision'] if promotion else baseline['revision'],
              'trace_sha256': current_hash, 'promotion_error': promotion_error,
              'progress': progress['state']}
    if args.result:
        loop.write(args.result, result)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
