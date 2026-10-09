import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import run


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((run.BASE / 'fixtures/repeated-claim.json').read_text())

    def load(self, value):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'submission.json'
            path.write_text(value if isinstance(value, str) else json.dumps(value))
            return run.read_submission(path)

    def test_rejects_non_data_payloads_and_oversized_traces(self):
        mutations = []
        for amount in (True, 0, -1, 1.5, 10**12 + 1, '1; revert();'):
            x = copy.deepcopy(self.fixture)
            x['actions'][0]['amount'] = amount
            mutations.append(x)
        x = copy.deepcopy(self.fixture)
        x['actions'][0]['op'] = 'shell'
        mutations.append(x)
        x = copy.deepcopy(self.fixture)
        x['actions'] *= 7
        mutations.append(x)
        x = copy.deepcopy(self.fixture)
        x['source'] = 'malicious code'
        mutations.append(x)
        for x in mutations:
            with self.subTest(x=x), self.assertRaises(ValueError):
                self.load(x)

    def test_duplicate_keys_rejected(self):
        with self.assertRaises(ValueError):
            self.load('{"schema_version":1,"schema_version":2}')

    def test_prose_never_enters_executable_source(self):
        self.fixture['description'] = '*/ contract Injected { /*'
        validated = self.load(self.fixture)
        self.assertNotIn(validated['description'], run.solidity_trace(validated))

    def test_replay_rejects_forged_concrete_outcomes(self):
        actions = [{'op': 'claim', 'pool': 'A', 'amount': 1}]
        with self.assertRaises(ValueError):
            run.replay_model(actions, ['claim_ok: 1'], fixed=True)

    def test_axiom_audit_fails_closed(self):
        with self.assertRaises(ValueError):
            run.check_axioms("'Foo' depends on axioms: [sorryAx]", {'Foo'})
        with self.assertRaises(ValueError):
            run.check_axioms('', {'Foo'})

    def test_valid_single_claim_has_no_objection(self):
        actions = [{'op': 'swap', 'pool': 'A', 'amount': 10000},
                   {'op': 'claim', 'pool': 'A', 'amount': 49}]
        logs = ['fee_A: 99', 'claim_ok: 1', 'A_collected: 99', 'A_earned: 49', 'A_paid: 49',
                'B_collected: 0', 'B_earned: 0', 'B_paid: 0', 'custody: 50']
        result, _ = run.replay_model(actions, logs, fixed=False)
        self.assertEqual(result['violations'], [])

    def test_setup_paths_are_used_by_local_preflight_with_explicit_override(self):
        with tempfile.TemporaryDirectory() as d, patch.object(run, 'ROOT', Path(d)):
            config = Path(d) / '.tools/v4/runtime.json'
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({'forge': '/pinned/forge', 'lean': '/pinned/lean',
                                          'solc': '/pinned/solc'}))
            with patch.dict('os.environ', {'V4_LEAN': '/custom/lean'}, clear=True):
                env = run.runtime_env()
            self.assertEqual(env['V4_FORGE'], '/pinned/forge')
            self.assertEqual(env['V4_LEAN'], '/custom/lean')
            self.assertEqual(env['V4_SOLC'], '/pinned/solc')


if __name__ == '__main__':
    unittest.main()
