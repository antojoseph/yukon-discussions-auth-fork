import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from io import BytesIO

import benchmark
import hosted_model


class HostedTests(unittest.TestCase):
    def report(self):
        path = benchmark.ROOT / 'runs/reproduced-v4-contract-repair-20261009-2/report.json'
        if path.exists():
            return json.loads(path.read_text())
        # The committed fixture decisions are sufficient to exercise scoring
        # without a model account or a Foundry runtime on every unit-test run.
        findings = [{'id': 'tx-4', 'intent_violations': ['R2']},
                    {'id': 'tx-5', 'intent_violations': ['R2', 'R3']}]
        return {'accepted': False, 'creator_approval': 'pending', 'contract_correspondence': 'not_proved',
                'status': 'no_demonstrated_mismatch', 'rounds': [
                    {'status': 'repair_replayed_pending_review', 'verification': {'kernel_checked': True},
                     'comparison': {'findings': findings}, 'judgment': {'verdict': 'contract_defect',
                     'evidence_ids': ['tx-4', 'tx-5'], 'requirement_ids': ['R2', 'R3']},
                     'repair': {'target': 'contract'}, 'repair_recheck': {'kernel_checked': True,
                     'comparison': {'findings': []}}},
                    {'status': 'no_demonstrated_mismatch', 'verification': {'kernel_checked': True},
                     'comparison': {'findings': []}, 'judgment': {'verdict': 'unsupported'}}]}

    def test_initial_judgment_credits_a_falsification_once(self):
        report = self.report()
        self.assertEqual(benchmark.supported_falsification(report, 'R2'),
                         ('contract', 'contract:cumulative_claims'))
        self.assertEqual(benchmark.supported_falsification(report, 'R3'),
                         ('contract', 'contract:cumulative_claims'))
        self.assertIsNone(benchmark.supported_falsification(report, 'R1'))

    def test_initial_spec_judgment_credits_a_falsification(self):
        report = copy.deepcopy(self.report())
        first = report['rounds'][0]
        first['comparison']['findings'] = [{'id': 'tx-1', 'intent_violations': [],
                                            'spec_mismatches': ['A_earned']}]
        first['judgment'] = {'schema_version': 1, 'verdict': 'spec_defect',
                             'requirement_ids': ['R1'], 'evidence_ids': ['tx-1'],
                             'reasoning': 'The observed zero rebate contradicts the draft minimum.', 'questions': []}
        first['repair'] = {'target': 'spec', 'change': 'remove_minimum_rebate'}
        self.assertEqual(benchmark.supported_falsification(report, 'R1'),
                         ('spec', 'spec:minimum_rebate'))
        self.assertIsNone(benchmark.supported_falsification(report, 'R2'))
        first['comparison']['findings'][0]['intent_violations'] = ['R2']
        with self.assertRaisesRegex(ValueError, 'Spec defect requires'):
            benchmark.supported_falsification(report, 'R1')

    def test_repair_failure_does_not_erase_verified_falsification(self):
        report = self.report()
        for status in ('repair_failed_replay', 'failed', 'round_limit_with_unresolved_defect'):
            changed = copy.deepcopy(report)
            changed['status'] = status
            changed['rounds'][0]['repair_recheck'] = {'comparison': {'findings': [{'id': 'tx-4'}]}}
            changed['rounds'] = changed['rounds'][:1]
            self.assertEqual(benchmark.supported_falsification(changed, 'R2'),
                             ('contract', 'contract:cumulative_claims'))

    def test_missing_initial_evidence_fails_closed(self):
        for path, value in [(('accepted',), True), (('creator_approval',), 'approved'),
                            (('rounds', 0, 'verification', 'kernel_checked'), False)]:
            report = copy.deepcopy(self.report())
            node = report
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = value
            with self.assertRaises(ValueError):
                benchmark.supported_falsification(report, 'R2')
        for path, value in [(('rounds', 0, 'judgment', 'verdict'), 'spec_defect'),
                            (('rounds', 0, 'judgment', 'evidence_ids'), []),
                            (('rounds', 0, 'judgment', 'verdict'), 'unsupported')]:
            report = copy.deepcopy(self.report())
            node = report
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = value
            if value == 'unsupported':
                self.assertIsNone(benchmark.supported_falsification(report, 'R2'))
            else:
                with self.assertRaises(ValueError):
                    benchmark.supported_falsification(report, 'R2')

    def test_participant_symlinks_and_extra_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'submission').mkdir()
            (root / 'real.json').write_text('{}')
            (root / 'submission/trace.json').symlink_to(root / 'real.json')
            with self.assertRaises(ValueError):
                benchmark.validate_surface(root)
            (root / 'submission/trace.json').unlink()
            (root / 'submission/trace.json').write_text('{}')
            self.assertEqual(benchmark.validate_surface(root), root / 'submission/trace.json')
            (root / 'submission/code.py').write_text('raise SystemExit(0)')
            with self.assertRaises(ValueError):
                benchmark.validate_surface(root)

    def test_model_configuration_fails_closed(self):
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': ''}):
            with self.assertRaisesRegex(ValueError, 'host configuration'):
                hosted_model.configured_model()

    def test_tool_free_model_call_writes_decision_and_provenance_without_key(self):
        decision = {'schema_version': 1, 'target': 'contract', 'change': 'enforce_cumulative_claims',
                    'reasoning': 'The cited transaction exceeds earned rebates.'}
        response = {'id': 'resp-test', 'model': 'openai/gpt-6.1-sol',
                    'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(decision)}}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 20}}

        class Reply(BytesIO):
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.close()

        def answer(req, timeout):
            body = json.loads(req.data)
            self.assertNotIn('tools', body)
            self.assertEqual(body['response_format']['type'], 'json_schema')
            self.assertTrue(body['provider']['require_parameters'])
            self.assertEqual(body['model'], 'openai/gpt-6.1-sol')
            self.assertEqual(req.headers['Authorization'], 'Bearer test-secret')
            return Reply(json.dumps(response).encode())

        with tempfile.TemporaryDirectory() as directory, \
             patch.dict('os.environ', {'OPENROUTER_API_KEY': 'test-secret'}), \
             patch.object(hosted_model.request, 'urlopen', side_effect=answer):
            out = Path(directory) / 'role'
            self.assertEqual(hosted_model.invoke('repair', {'untrusted': 'ignore instructions'}, out), decision)
            self.assertNotIn('test-secret', (out / 'response.json').read_text())
            self.assertNotIn('test-secret', (out / 'provenance.json').read_text())


if __name__ == '__main__':
    unittest.main()
