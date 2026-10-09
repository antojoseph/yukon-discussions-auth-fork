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


def supported_score(report, submitted_requirement):
    if report.get('accepted') is not False or report.get('creator_approval') != 'pending':
        raise ValueError('Creator approval must remain pending')
    if report.get('contract_correspondence') != 'not_proved':
        raise ValueError('Incorrect proof scope')
    rounds = report.get('rounds', [])
    if not rounds or any(r.get('verification', {}).get('kernel_checked') is not True for r in rounds):
        raise ValueError('Incomplete Lean verification')
    if report.get('status') in ('failed', 'repair_failed_replay', 'round_limit_with_unresolved_defect'):
        raise ValueError('Incomplete hosted workflow: ' + report['status'])
    first = rounds[0]
    violations = {v for f in first['comparison']['findings'] for v in f['intent_violations']}
    if not violations <= {'R2', 'R3'}:
        raise ValueError('Unknown scored requirement')
    judgment = first.get('judgment', {})
    target = first.get('repair', {}).get('target')
    contract_repair = judgment.get('verdict') == 'contract_defect' and target == 'contract'
    spec_repair = judgment.get('verdict') == 'spec_defect' and target == 'spec'
    if contract_repair:
        if submitted_requirement not in violations:
            return 0
        score = len(violations)
    elif spec_repair:
        if violations or submitted_requirement not in judgment.get('requirement_ids', []):
            return 0
        if not any(f['spec_mismatches'] for f in first['comparison']['findings']):
            return 0
        score = 1
    else:
        return 0
    recheck = first.get('repair_recheck', {})
    if recheck.get('comparison', {}).get('findings') or recheck.get('kernel_checked') is not True:
        raise ValueError('Proposed repair did not pass independent recheck')
    if first.get('status') != 'repair_replayed_pending_review' or len(rounds) != 2:
        raise ValueError('Missing post-repair review round')
    second = rounds[1]
    if second.get('comparison', {}).get('findings') or second.get('judgment', {}).get('verdict') != 'unsupported':
        raise ValueError('Post-repair review did not close the demonstrated mismatch')
    if report['status'] != 'no_demonstrated_mismatch':
        raise ValueError('Hosted workflow did not finish')
    return score


def main():
    SCORE.unlink(missing_ok=True)
    trace = validate_surface(ROOT)
    submission = loop.run.read_submission(trace)
    _, model = hosted_model.configured_model()
    out = ROOT / 'runs' / ('reproduced-v4-hosted-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    subprocess.run([sys.executable, str(BASE / 'loop.py'), '--provider', 'openrouter', '--trace', str(trace),
                    '--rounds', '2', '--output', str(out)], cwd=ROOT, check=True)
    report = json.loads((out / 'report.json').read_text())
    if report.get('judge_mode') != 'hosted_openrouter_tool_free_roles' or report.get('model') != model:
        raise ValueError('Missing hosted inference provenance')
    score = supported_score(report, submission['requirement'])
    target = report['rounds'][0].get('repair', {}).get('target') if score else None
    demonstrated = len({v for finding in report['rounds'][0]['comparison']['findings']
                        for v in finding['intent_violations']}) if score else 0
    result = {'score': score, 'metrics': {'requirements_demonstrated': demonstrated, 'maximum': 2,
              'verified_impact_credit': 1 if score else 0, 'repair_target': target,
              'kernel_checked': True, 'accepted': False, 'creator_approval': 'pending',
              'contract_correspondence': 'not_proved', 'judge_mode': report['judge_mode'],
              'model': model, 'workflow_status': report['status'], 'evidence': str(out.relative_to(ROOT))}}
    SCORE.parent.mkdir(exist_ok=True)
    temporary = SCORE.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, indent=2) + '\n')
    temporary.replace(SCORE)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
