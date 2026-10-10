#!/usr/bin/env python3
"""Hosted participant workflow: transaction -> judgment -> repair -> replay."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

import hosted_model
import loop

ROOT = Path(__file__).resolve().parents[2]
BASE = Path(__file__).resolve().parent
SCORE = ROOT / '.yukon/score.json'


def validate_surface(root):
    directory = root / 'submission'
    trace = directory / 'trace.json'
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('Submission must be a directory')
    if trace.is_symlink() or not trace.is_file():
        raise ValueError('Submission must be a regular trace.json file')
    if sorted(p.name for p in directory.iterdir()) != ['trace.json']:
        raise ValueError('Only submission/trace.json is permitted')
    return trace


def supported_falsification(report, submitted_requirement):
    if report.get('accepted') is not False or report.get('creator_approval') != 'pending':
        raise ValueError('Creator approval must remain pending')
    if report.get('contract_correspondence') != 'not_proved':
        raise ValueError('Incorrect proof scope')
    rounds = report.get('rounds', [])
    if not rounds or (rounds[0].get('verification', {}).get('kernel_checked') is not True and
                      rounds[0].get('verification', {}).get('evm_executed') is not True):
        raise ValueError('Missing initial execution or Lean verification')
    first = rounds[0]
    findings = first['comparison']['findings']
    violations = {v for f in findings for v in f['intent_violations']}
    transaction_ids = [tx['id'] for tx in first.get('execution', {}).get('transactions', [])]
    judgment = loop.validate_judgment(first.get('judgment', {}), first['comparison'], transaction_ids or None)
    cited = [f for f in findings if f['id'] in judgment.get('evidence_ids', [])]
    if submitted_requirement not in judgment.get('requirement_ids', []):
        return None
    if judgment.get('verdict') == 'contract_defect':
        if judgment.get('defect_key'):
            return 'contract', judgment['defect_key']
        if any(submitted_requirement in f['intent_violations'] for f in cited):
            return 'contract', 'contract:cumulative_claims'
    elif judgment.get('verdict') == 'spec_defect':
        if judgment.get('defect_key'):
            return 'spec', judgment['defect_key']
        if not violations and any(f.get('spec_mismatches') for f in cited):
            return 'spec', 'spec:minimum_rebate'
    elif judgment.get('verdict') == 'both' and judgment.get('defect_key'):
        return 'both', judgment['defect_key']
    return None


def main():
    SCORE.unlink(missing_ok=True)
    trace = validate_surface(ROOT)
    submission = loop.run.read_submission(trace)
    _, model = hosted_model.configured_model()
    out = ROOT / 'runs' / ('reproduced-v4-hosted-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    process = subprocess.run([sys.executable, str(BASE / 'loop.py'), '--provider', 'openrouter', '--trace', str(trace),
                              '--rounds', '2', '--output', str(out)], cwd=ROOT, check=False)
    report = json.loads((out / 'report.json').read_text())
    if report.get('judge_mode') != 'hosted_openrouter_tool_free_roles' or report.get('model') != model:
        raise ValueError('Missing hosted inference provenance')
    finding = supported_falsification(report, submission['requirement'])
    if process.returncode and finding is None:
        raise ValueError('Hosted workflow failed before a supported falsification was verified')
    score = 1 if finding else 0
    target, defect_key = finding if finding else (None, None)
    demonstrated = len({v for finding in report['rounds'][0]['comparison']['findings']
                        for v in finding['intent_violations']}) if score else 0
    result = {'score': score, 'metrics': {'requirements_demonstrated': demonstrated,
              'verified_falsification': score, 'defect_key': defect_key, 'repair_target': target,
              'host_repair_verified': report['status'] in ('no_demonstrated_mismatch', 'extension_verified'),
              'spec_gap': report['rounds'][0]['judgment'].get('spec_gap', False),
              'kernel_checked': report['rounds'][0]['verification']['kernel_checked'],
              'model_kernel_checked': report['rounds'][0].get('extension_recheck', {}).get('formal', {}).get('model_kernel_checked', False),
              'evm_executed': report['rounds'][0]['verification'].get('evm_executed', False),
              'accepted': False, 'creator_approval': 'pending',
              'contract_correspondence': 'not_proved', 'judge_mode': report['judge_mode'],
              'model': model, 'workflow_status': report['status'], 'evidence': str(out.relative_to(ROOT))}}
    SCORE.parent.mkdir(exist_ok=True)
    temporary = SCORE.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, indent=2) + '\n')
    temporary.replace(SCORE)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
