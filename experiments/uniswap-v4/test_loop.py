import copy
import json
import unittest
import loop
from execution import compare, patch_contract


class RepairRoutingTests(unittest.TestCase):
    def setUp(self):
        self.spec = loop.validate_spec(json.loads((loop.run.BASE / 'specs/base.json').read_text()))
        self.source = (loop.run.BASE / 'src/RebateHook.sol').read_text()
        self.findings = {'findings': [{'id': 'tx-4'}, {'id': 'tx-5'}]}
        self.judge = json.loads((loop.run.BASE / 'fixtures/contract-repair-responses.json').read_text())[0]['judge']

    def test_judge_cannot_cite_unobserved_or_unmapped_violation(self):
        j = copy.deepcopy(self.judge)
        j['evidence_ids'] = ['tx-999']
        with self.assertRaisesRegex(ValueError, 'does not demonstrate'):
            loop.validate_judgment(j, self.findings)
        j = copy.deepcopy(self.judge)
        j['requirement_ids'] = []
        with self.assertRaisesRegex(ValueError, 'citations'):
            loop.validate_judgment(j, self.findings)

    def test_repair_cannot_change_other_artifact_or_noop(self):
        p = {'schema_version': 1, 'target': 'spec', 'change': 'cumulative', 'reasoning': 'Change policy'}
        with self.assertRaisesRegex(ValueError, 'target'):
            loop.validate_repair(p, self.judge, self.spec, 'seeded')
        j = {**self.judge, 'verdict': 'spec_defect'}
        with self.assertRaisesRegex(ValueError, 'no-op'):
            loop.validate_repair(p, j, self.spec, 'repaired')

    def test_contract_patch_removes_seeded_per_call_guard(self):
        proposed = patch_contract(self.source)
        self.assertNotIn('if (amount > entitlement) revert InvalidClaim();', proposed)
        self.assertIn('if (amount > entitlement - claimed[pool][currency][msg.sender])', proposed)
        self.assertIn('if (amount > collected[pool][currency] - paid[pool][currency])', proposed)

    def test_comparison_detects_spec_defect_with_no_intent_violation(self):
        observed = {'transactions': [
            {'id': 'tx-1', 'action': {'op': 'swap', 'pool': 'A', 'amount': 10000}, 'succeeded': True, 'fee': 100,
             'after': {'A_collected': 100, 'A_earned': 50, 'A_paid': 0, 'B_collected': 0, 'B_earned': 0, 'B_paid': 0, 'custody': 100}},
            {'id': 'tx-2', 'action': {'op': 'claim', 'pool': 'A', 'amount': 50}, 'succeeded': True, 'fee': None,
             'after': {'A_collected': 100, 'A_earned': 50, 'A_paid': 50, 'B_collected': 0, 'B_earned': 0, 'B_paid': 0, 'custody': 50}},
            {'id': 'tx-3', 'action': {'op': 'claim', 'pool': 'A', 'amount': 50}, 'succeeded': False, 'fee': None,
             'after': {'A_collected': 100, 'A_earned': 50, 'A_paid': 50, 'B_collected': 0, 'B_earned': 0, 'B_paid': 0, 'custody': 50}},
        ]}
        differences = compare({'claim_limit': 'per_call'}, observed)['findings']
        self.assertEqual(differences[0]['id'], 'tx-3')
        self.assertEqual(differences[0]['intent_violations'], [])
        self.assertIn('call_outcome', differences[0]['spec_mismatches'])


if __name__ == '__main__':
    unittest.main()
