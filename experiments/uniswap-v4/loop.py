#!/usr/bin/env python3
"""Base spec + contract -> concrete challenge -> independent judge -> proposed repair."""
import argparse
import difflib
import hashlib
import html
import json
import re
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

import run
import hosted_model
from execution import compare, digest, execute, patch_contract, runtime_env
sys.path.insert(0, str(run.ROOT))
from app import invoke_agent


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')


def read(path):
    if path.stat().st_size > 262144:
        raise ValueError('Structured input exceeds 256 KiB')
    return json.loads(path.read_text(), object_pairs_hook=run.no_duplicates)


def validate_spec(spec):
    if not isinstance(spec, dict) or set(spec) != {'schema_version', 'family', 'claim_limit', 'minimum_rebate'}:
        raise ValueError('Invalid base specification fields')
    if type(spec['schema_version']) is not int or spec['schema_version'] != 1 or spec['family'] != 'uniswap-v4-rebate-v1':
        raise ValueError('Unsupported specification version/family')
    if spec['claim_limit'] not in ('cumulative', 'per_call'):
        raise ValueError('Unsupported claim interpretation')
    if type(spec['minimum_rebate']) is not int or spec['minimum_rebate'] not in (0, 1):
        raise ValueError('Unsupported minimum rebate interpretation')
    return spec


def validate_judgment(j, comparison, transaction_ids=None):
    fields = {'schema_version', 'verdict', 'evidence_ids', 'requirement_ids', 'reasoning', 'questions'}
    optional = {'defect_key', 'spec_gap'}
    if not isinstance(j, dict) or not fields <= set(j) or not set(j) <= fields | optional or type(j['schema_version']) is not int or j['schema_version'] != 1:
        raise ValueError('Invalid judgment fields/version')
    if j['verdict'] not in ('contract_defect', 'spec_defect', 'both', 'ambiguous', 'unsupported'):
        raise ValueError('Unknown judgment')
    if not isinstance(j['reasoning'], str) or not j['reasoning'].strip() or len(j['reasoning']) > 20000:
        raise ValueError('Judge must explain its reasoning')
    for key in ('evidence_ids', 'requirement_ids', 'questions'):
        if not isinstance(j[key], list) or any(not isinstance(x, str) for x in j[key]) or len(j[key]) > 32:
            raise ValueError('Invalid judge citations/questions')
    if not set(j['requirement_ids']) <= {'R1', 'R2', 'R3', 'R4', 'R5'}:
        raise ValueError('Unknown intent requirement citation')
    ids = set(transaction_ids) if transaction_ids is not None else {f['id'] for f in comparison['findings']}
    if not set(j['evidence_ids']) <= ids:
        raise ValueError('Judge cited a transaction that was not executed')
    key = j.get('defect_key', '')
    if 'spec_gap' in j and type(j['spec_gap']) is not bool:
        raise ValueError('Invalid Lean specification gap flag')
    if j.get('spec_gap') and j['verdict'] not in ('spec_defect', 'both'):
        raise ValueError('Lean specification gap requires a specification verdict')
    if j.get('spec_gap') and not key:
        raise ValueError('Lean specification gap requires a defect key')
    target = {'contract_defect': 'contract', 'spec_defect': 'spec', 'both': 'both'}.get(j['verdict'])
    if not isinstance(key, str) or (key and not re.fullmatch(r'(contract|spec|both):[a-z][a-z0-9_]{2,79}', key)):
        raise ValueError('Invalid defect key')
    if key and (target is None or not key.startswith(target + ':')):
        raise ValueError('Defect key does not match judgment target')
    if j['verdict'] in ('contract_defect', 'spec_defect', 'both'):
        if not j['evidence_ids'] or not j['requirement_ids']:
            raise ValueError('Defect judgments require concrete evidence and requirement citations')
        if j['questions']:
            raise ValueError('Unresolved questions require ambiguous adjudication')
        cited = [f for f in comparison['findings'] if f['id'] in j['evidence_ids']]
        supported_requirements = {requirement for f in cited for requirement in f['intent_violations']}
        novel_evidence = (key not in ('', 'contract:cumulative_claims', 'spec:minimum_rebate') or
                          any(evidence_id not in {f['id'] for f in comparison['findings']}
                              for evidence_id in j['evidence_ids']))
        if j['verdict'] == 'contract_defect' and not novel_evidence and not set(j['requirement_ids']) <= supported_requirements:
            raise ValueError('Judge requirement citation lacks observed intent evidence')
        if j['verdict'] == 'contract_defect' and not novel_evidence and not any(f['intent_violations'] for f in cited):
            raise ValueError('Contract defect requires an observed intent violation')
        if j['verdict'] == 'spec_defect' and not novel_evidence and (not any(f['spec_mismatches'] for f in cited)
                                               or any(f['intent_violations'] for f in cited)):
            raise ValueError('Spec defect requires spec disagreement without observed intent violation')
    return j


def validate_repair(proposal, judgment, spec, contract):
    if not isinstance(proposal, dict) or set(proposal) != {'schema_version', 'target', 'change', 'reasoning'}:
        raise ValueError('Invalid repair fields')
    if type(proposal['schema_version']) is not int or proposal['schema_version'] != 1:
        raise ValueError('Invalid repair version')
    target = {'contract_defect': 'contract', 'spec_defect': 'spec'}.get(judgment['verdict'])
    if target is None or proposal['target'] != target:
        raise ValueError('Repair cannot change a target the judge did not authorize')
    allowed_change = (proposal['change'] == 'enforce_cumulative_claims' and contract == 'seeded') if target == 'contract' else (
        (proposal['change'] == 'cumulative' and spec['claim_limit'] == 'per_call') or
        (proposal['change'] == 'remove_minimum_rebate' and spec['minimum_rebate'] == 1))
    if not allowed_change or not isinstance(proposal['reasoning'], str) or not proposal['reasoning'].strip():
        raise ValueError('Unsupported repair or missing rationale')
    return proposal


def agent(role, payload, out, args):
    if args.provider == 'openrouter':
        return hosted_model.invoke(role, payload, out, args.timeout)
    return invoke_agent(role, payload, out, args.timeout, model=args.model,
                        instruction_root=run.BASE, schema_name=role + '.json')


def proof(submission_path, out):
    # Existing verifier independently runs both variants, model agreement, Lean,
    # and the original regression suite. It never receives an LLM proof/source.
    code, logs = run.command([sys.executable, run.BASE / 'run.py', submission_path, '--output', out],
                             env=runtime_env(), timeout=600)
    if code:
        raise ValueError('Independent EVM/Lean verification failed: ' + logs[-2000:])
    evidence = read(out / 'evidence.json')
    for key in ('validation_completed', 'kernel_checked', 'concrete_abstract_replay_agree'):
        if evidence.get(key) is not True:
            raise ValueError('Missing independent verification: ' + key)
    return evidence


def render(state):
    esc = lambda x: html.escape(str(x))
    sections = []
    for r in state['rounds']:
        evidence = r.get('execution', {})
        transactions = evidence.get('transactions', [])
        predictions = r.get('comparison', {}).get('predictions', [])
        rows = ''.join('<tr><td>' + esc(tx['id']) + '</td><td>' +
            (esc(tx['action']['op']) + ' ' + esc(tx['action']['pool']) + ' · ' + str(tx['action']['amount'])
             if tx['action']['op'] != 'call' else 'call ' + esc(tx['action']['target']) + ' as ' + esc(tx['action']['caller'])) +
            '</td><td>' + ('success' if tx['succeeded'] else 'revert') +
            '</td><td>' + ('unmodeled' if p.get('unmodeled') else 'success' if p['succeeded'] else 'reject') +
            '</td><td>' + str(tx['after']['A_paid']) +
            '</td><td>' + str(tx['after']['custody']) + '</td></tr>' for tx, p in zip(transactions, predictions))
        j = r.get('judgment', {})
        repair = r.get('repair', {})
        sections.append(f'''<section><h2>Round {r['number']} · {esc(r.get('status', 'in progress'))}</h2>
<p>Base specification: <b>{esc(r['spec']['claim_limit'])}</b> claims, minimum rebate <b>{esc(r['spec']['minimum_rebate'])}</b> · Contract: <b>{esc(r['contract'])}</b></p>
<table><tr><th>Evidence</th><th>Call</th><th>EVM outcome</th><th>Spec prediction</th><th>A paid</th><th>Custody</th></tr>{rows}</table>
<h3>Independent judge · {esc(j.get('verdict', 'not run'))}</h3><p>{esc(j.get('reasoning', 'Execution and proof checks must complete first.'))}</p>
<p>Citations: {esc(', '.join(j.get('evidence_ids', [])))} · {esc(', '.join(j.get('requirement_ids', [])))}</p>
<h3>Proposed repair · {esc(repair.get('target', 'none'))}</h3><p>{esc(repair.get('reasoning', 'No repair proposed.'))}</p>
<p><a href="round-{r['number']}/spec.json">Specification</a> · <a href="round-{r['number']}/contract.sol">Contract source</a> ·
<a href="round-{r['number']}/execution/forge-transactions.json">Raw EVM replay</a></p>
<details><summary>Versions, findings, rechecks and proof scope</summary><pre>{esc(json.dumps(r, indent=2))}</pre></details></section>''')
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>spec.prove · falsification and repair</title><style>body{{max-width:1060px;margin:40px auto;padding:0 24px;background:#fafbf8;color:#18312e;font:16px/1.6 system-ui}}h1{{font-size:36px;line-height:1.2}}section{{margin:32px 0;border-top:1px solid #ccd8d0}}table{{width:100%;border-collapse:collapse}}td,th{{text-align:left;padding:10px;border-bottom:1px solid #ddd}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#eaf4ef;padding:16px}}a{{color:#175e49}}.flow{{padding:18px;background:#eaf4ef}}</style>
<p>spec.prove · bounded Uniswap v4 experiment</p><h1>Falsify the behavior. Repair the right artifact.</h1>
<p class="flow">Base spec + contract → executed transaction evidence → independent LLM judge → proposed spec revision or contract patch → replay and re-review</p>
<p>Status: <b>{esc(state['status'])}</b>. Creator approval: <b>pending</b>. Accepted: <b>false</b>.</p>
<p>Calls execute inside the local Foundry EVM, against the pinned PoolManager. These are not mined external-chain transactions. Lean checks the accounting abstraction; EVM correspondence and English fidelity are not proved.</p>
<p>Judge provenance: {esc(state['judge_mode'])}. Spec changes remain proposals and cannot silently redefine creator intent.</p>
{''.join(sections)}<p>{esc(state.get('error', ''))}</p><p><a href="report.json">Complete evidence and provenance JSON</a></p></html>'''


def save(out, state):
    write(out / 'report.json', state)
    (out / 'report.html').write_text(render(state))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', type=Path, default=run.BASE / 'specs/base.json')
    parser.add_argument('--contract', choices=['seeded', 'repaired'])
    parser.add_argument('--trace', type=Path, help='Optional existing counterexample; otherwise an independent challenger proposes one each round')
    parser.add_argument('--responses', type=Path, help='Replay explicitly labeled saved judge/repair responses; no inference')
    parser.add_argument('--rounds', type=int, choices=range(1, 4), default=2)
    parser.add_argument('--model')
    parser.add_argument('--provider', choices=['codex', 'openrouter'], default='codex')
    parser.add_argument('--timeout', type=int, default=240)
    parser.add_argument('--output', type=Path, default=run.ROOT / 'runs' / ('reproduced-v4-loop-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')))
    args = parser.parse_args()
    baseline = read(run.BASE / 'baseline.json')
    if (not isinstance(baseline, dict) or set(baseline) !=
            {'schema_version', 'revision', 'contract', 'contract_source'} or
            baseline['schema_version'] != 1 or type(baseline['revision']) is not int or baseline['revision'] < 0 or
            (baseline['contract'], baseline['contract_source']) not in
            (('seeded', 'src/RebateHook.sol'), ('patched', 'specs/active/RebateHook.sol'))):
        raise ValueError('Invalid active baseline')
    spec = validate_spec(read(args.spec)); contract = args.contract or baseline['contract']
    source = (run.BASE / (baseline['contract_source'] if args.contract is None else 'src/RebateHook.sol')).read_text()
    intent = read(run.BASE / 'intent.json')
    responses = read(args.responses) if args.responses else None
    out = args.output.resolve()
    if out.parent != (run.ROOT / 'runs').resolve() or not out.name.startswith(('reproduced-', 'live-')):
        raise ValueError('Use a new runs/reproduced-* or runs/live-* directory')
    out.mkdir(exist_ok=False)
    state = dict(schema_version=1, status='running', accepted=False, creator_approval='pending',
        baseline_revision=baseline['revision'],
        contract_correspondence='not_proved', deployment='none', intent=intent, intent_sha256=digest(intent),
        judge_mode='saved_responses_no_fresh_inference' if responses else
                   ('hosted_openrouter_tool_free_roles' if args.provider == 'openrouter' else 'live_independent_codex_contexts'),
        model=(hosted_model.configured_model()[1] if args.provider == 'openrouter' and not responses else args.model or 'configured_default'), rounds=[])
    parent = None
    try:
        for i in range(args.rounds):
            number = i + 1; folder = out / f'round-{number}'
            folder.mkdir()
            version = digest(dict(spec=spec, contract=contract, source=source, intent_sha256=state['intent_sha256']))
            r = dict(number=number, spec=spec.copy(), contract=contract, version=version, parent_version=parent, status='executing')
            state['rounds'].append(r)
            write(folder / 'spec.json', spec); (folder / 'contract.sol').write_text(source)
            payload = dict(intent=intent, specification=spec, contract_source=source, contract_configuration=contract)
            if args.trace:
                submission = run.read_submission(args.trace)
            elif responses:
                raise ValueError('Saved response replay requires an explicit trace')
            else:
                submission = agent('challenger', payload, folder / 'challenger', args)
            write(folder / 'trace.json', submission)
            submission = run.read_submission(folder / 'trace.json')
            print(f'Round {number}: execute concrete calls' +
                  ('' if any(action['op'] == 'call' for action in submission['actions']) else ' and independently check Lean'),
                  flush=True)
            observed = execute(submission, contract != 'seeded', source, folder / 'execution', patched=contract == 'patched')
            r['execution'] = observed
            actual_model = compare({'claim_limit': 'per_call' if contract == 'seeded' else 'cumulative'}, observed)
            r['contract_model_comparison'] = actual_model
            proof_out = run.ROOT / 'runs' / ('reproduced-proof-' + out.name + f'-{number}')
            if any(action['op'] == 'call' for action in submission['actions']):
                formal = {'kernel_checked': False}
                proof_error = 'Raw local calls are outside the current Lean trace model'
            else:
                try:
                    formal = proof(folder / 'trace.json', proof_out)
                    proof_error = None
                except ValueError as error:
                    formal = {'kernel_checked': False}
                    proof_error = str(error)
            r['verification'] = dict(kernel_checked=formal['kernel_checked'], evm_executed=True,
                formal_error=proof_error,
                reference_checks=str(proof_out.relative_to(run.ROOT)) if proof_out.exists() else None,
                contract_correspondence='not_proved', abstract_all_traces_invariant_proved=formal['kernel_checked'],
                exact_proposal_regressions_passed=observed['regressions_passed'])
            r['comparison'] = compare(spec, observed)
            write(folder / 'comparison.json', r['comparison'])
            payload.update(submission=submission, concrete_evidence=observed, comparison=r['comparison'], formal_scope=r['verification'])
            payload['allowed_evidence_ids'] = [tx['id'] for tx in observed['transactions']]
            print(f'Round {number}: independent semantic judgment', flush=True)
            if responses:
                judgment = responses[i]['judge']
                r['judgment'] = validate_judgment(judgment, r['comparison'], payload['allowed_evidence_ids'])
            else:
                for attempt in range(2):
                    judgment = agent('judge', payload, folder / ('judge-attempt-' + str(attempt + 1)), args)
                    try:
                        r['judgment'] = validate_judgment(judgment, r['comparison'], payload['allowed_evidence_ids'])
                        break
                    except ValueError as error:
                        if attempt:
                            raise
                        payload['previous_judgment'] = judgment
                        payload['citation_correction'] = str(error) + '; only allowed_evidence_ids may appear in evidence_ids'
                payload.pop('previous_judgment', None)
                payload.pop('citation_correction', None)
            write(folder / 'judgment.json', judgment)
            if judgment.get('spec_gap') and judgment['verdict'] in ('spec_defect', 'both'):
                request = dict(schema_version=1, baseline_revision=state['baseline_revision'],
                    defect_key=judgment['defect_key'], evidence_ids=judgment['evidence_ids'],
                    requirement_ids=judgment['requirement_ids'], reasoning=judgment['reasoning'],
                    trace=folder.joinpath('trace.json').relative_to(run.ROOT).as_posix(),
                    required_artifacts=['specs/base.json', 'lean/Rebate.lean'],
                    status='pending_spec_extension_review')
                r['spec_extension_request'] = request
                write(folder / 'spec-extension-request.json', request)
                r['status'] = state['status'] = ('spec_extension_required' if judgment['verdict'] == 'spec_defect'
                                                else 'needs_creator_review')
                break
            if judgment['verdict'] in ('both', 'ambiguous'):
                r['status'] = state['status'] = 'needs_creator_review'; break
            if judgment['verdict'] == 'unsupported':
                r['status'] = state['status'] = 'no_demonstrated_mismatch' if not r['comparison']['findings'] else 'judge_disagrees_with_finding'
                break
            if number == args.rounds:
                r['status'] = state['status'] = 'round_limit_with_unresolved_defect'; break
            proposal = responses[i]['repair'] if responses else agent('repair', {**payload, 'judgment': judgment}, folder / 'proposer', args)
            try:
                r['repair'] = validate_repair(proposal, judgment, spec, contract)
            except ValueError as error:
                r['repair'] = proposal
                r['status'] = state['status'] = 'repair_requires_review'
                r['repair_error'] = str(error)
                write(folder / 'repair.json', proposal)
                break
            write(folder / 'repair.json', proposal)
            parent = version
            if proposal['target'] == 'contract':
                patched = patch_contract(source)
                (folder / 'contract.patch').write_text(''.join(difflib.unified_diff(source.splitlines(True), patched.splitlines(True), fromfile='base/RebateHook.sol', tofile='proposed/RebateHook.sol')))
                source = patched; contract = 'patched'
                (folder / 'proposed.sol').write_text(source)
            else:
                spec = {**spec, 'claim_limit': 'cumulative'} if proposal['change'] == 'cumulative' else {
                    **spec, 'minimum_rebate': 0}
                write(folder / 'proposed-spec.json', spec)
            # Replay the identical falsification before any fresh challenger gets a turn.
            print(f'Round {number}: recheck proposed {proposal["target"]} repair on the same calls', flush=True)
            recheck = execute(submission, contract != 'seeded', source, folder / 'repair-recheck', patched=contract == 'patched')
            comparison = compare(spec, recheck)
            r['repair_recheck'] = dict(execution=recheck, comparison=comparison, kernel_checked=formal['kernel_checked'],
                proof_scope='Existing cumulative accounting invariant and submitted witness; no EVM correspondence theorem')
            if comparison['findings']:
                r['status'] = state['status'] = 'repair_failed_replay'; break
            r['status'] = 'repair_replayed_pending_review'
            save(out, state)
        else:
            state['status'] = 'round_limit'
    except Exception as exc:
        state['status'] = 'failed'; state['error'] = str(exc)
        if state['rounds']:
            state['rounds'][-1]['status'] = 'failed'
        raise
    finally:
        save(out, state)
    print(json.dumps(dict(status=state['status'], accepted=False, creator_approval='pending', report=str(out / 'report.html')), indent=2))


if __name__ == '__main__':
    main()
