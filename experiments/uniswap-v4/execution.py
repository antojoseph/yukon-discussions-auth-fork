"""Instrumented local EVM calls. Only validated enums/integers enter Solidity."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import run


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def patch_contract(source):
    start = source.index('        if (repaired) {')
    end = source.index('        claimed[pool][currency][msg.sender] += amount;', start)
    return source[:start] + '''        // Proposed repair: cumulative entitlement and pool-local backing.
        if (amount > entitlement - claimed[pool][currency][msg.sender]) revert InvalidClaim();
        if (amount > collected[pool][currency] - paid[pool][currency]) revert InvalidClaim();
''' + source[end:]


def instrumented_source(submission, fixed):
    source = run.solidity_trace(submission)
    source = source.replace('contract SubmissionReplay', 'contract TransactionReplay')
    source = source.replace('    function testSubmittedSeeded() public { fixture(false); replay(); }\n    function testSubmittedRepaired() public { fixture(true); replay(); }',
        '    function testTransactionReplay() public { fixture(' + str(fixed).lower() + '); observe(); replay(); }')
    # Every action is followed by independent storage/balance observations.
    source = source.replace('true));', 'true)); observe();')
    source = source.replace('ok ? 1 : 0); }', 'ok ? 1 : 0); emit log_named_bytes("return_data", data); observe(); }')
    source = source.replace('(bool ok,) = address(hook).call', '(bool ok, bytes memory data) = address(hook).call')
    source = source.replace('emit log_named_bytes("raw_return_data", data); }',
                            'emit log_named_bytes("raw_return_data", data); observe(); }')
    # End-of-replay summary belongs to the old interface; observe covers it per step.
    source = source[:source.index('emit log_named_uint("A_collected"')]
    observe = []
    for pool in ('A', 'B'):
        for field in ('collected', 'earned', 'paid', 'claimed'):
            extra = ', address(this)' if field in ('earned', 'claimed') else ''
            observe.append(f'emit log_named_uint("{pool}_{field}", hook.{field}(pool{pool}.toId(), currency1{extra}));')
    observe += ['emit log_named_uint("custody", currency1.balanceOf(address(hook)));',
                'emit log_named_uint("recipient_balance", currency1.balanceOf(address(this)));',
                'emit log_named_address("caller", address(this));',
                'emit log_named_address("target", address(hook));',
                'emit log_named_uint("timestamp", block.timestamp);']
    return source + '\n}\nfunction observe() private {\n' + '\n'.join(observe) + '\n}\n}\n'


def runtime_env():
    return run.runtime_env()


def execute(submission, fixed, source, out, patched=False, run_regressions=True):
    """Compile the exact recorded source in an isolated fixture and inspect actual calls."""
    out.mkdir(parents=True, exist_ok=False)
    run.verify_dependencies()
    env = runtime_env()
    generated = instrumented_source(submission, fixed)
    (out / 'TransactionReplay.t.sol').write_text(generated)
    (out / 'RebateHook.sol').write_text(source)
    with tempfile.TemporaryDirectory(prefix='v4-transaction-') as directory:
        root = Path(directory)
        shutil.copytree(run.BASE / 'src', root / 'src')
        shutil.copytree(run.BASE / 'test', root / 'test', ignore=shutil.ignore_patterns('Submission_*'))
        (root / 'src/RebateHook.sol').write_text(source)
        (root / 'test/TransactionReplay.t.sol').write_text(generated)
        shutil.copyfile(run.BASE / 'foundry.toml', root / 'foundry.toml')
        (root / 'lib').symlink_to(run.BASE / 'lib', target_is_directory=True)
        args = [env.get('V4_FORGE', 'forge'), 'test', '--root', root, '--json', '-vv']
        if env.get('V4_SOLC'):
            args += ['--use', env['V4_SOLC'], '--offline']
        code, raw = run.command(args + ['--match-contract', '^TransactionReplay$', '--match-test', r'^testTransactionReplay\(\)$'], cwd=root, env=env)
        (out / 'forge-transactions.json').write_text(raw)
        if code:
            raise ValueError('Concrete execution failed; inspect forge-transactions.json')
        tests = run.decode_forge(raw)
        if len(tests) != 1:
            raise ValueError('Expected exactly one instrumented replay')
        logs = next(iter(tests.values()))['decoded_logs']
        regression_count = None
        if patched and run_regressions:
            # The historical seeded-attack test asserts the bug. All ten behavior
            # regressions must instead pass against the exact proposed source.
            code, raw = run.command(args + ['--match-contract', '^RebateHookTest$',
                '--no-match-test', r'^testSeededAttackDrainsOtherPoolReserve\(\)$'], cwd=root, env=env)
            (out / 'forge-patch-regressions.json').write_text(raw)
            if code:
                raise ValueError('Proposed contract failed regressions')
            regression_count = len(run.decode_forge(raw))
            if regression_count != 10:
                raise ValueError('Expected all ten contract behavior regressions')
    iterator = iter(logs)
    fields = [p + '_' + f for p in ('A', 'B') for f in ('collected', 'earned', 'paid', 'claimed')]
    fields += ['custody', 'recipient_balance', 'caller', 'target', 'timestamp']
    def take(key):
        try:
            label, value = next(iterator).split(': ', 1)
        except (StopIteration, ValueError):
            raise ValueError('Missing concrete observation: ' + key)
        if label != key:
            raise ValueError('Unexpected concrete observation: ' + label)
        return value
    def observe():
        return {key: take(key) if key in ('caller', 'target') else int(take(key)) for key in fields}
    initial = observe()
    previous = initial
    events = []
    for i, action in enumerate(submission['actions']):
        if action['op'] == 'swap':
            fee, success, returned = int(take('fee_' + action['pool'])), True, None
        elif action['op'] == 'call':
            fee, success, returned = None, int(take('raw_ok')), take('raw_return_data')
            if success not in (0, 1):
                raise ValueError('Invalid EVM call result')
        else:
            fee, success, returned = None, int(take('claim_ok')), take('return_data')
            if success not in (0, 1):
                raise ValueError('Invalid EVM call result')
        after = observe()
        anomalies = []
        if not success and after != previous:
            anomalies.append('rejected_call_changed_state')
        if any(after[p + '_claimed'] != after[p + '_paid'] for p in ('A', 'B')):
            anomalies.append('claim_accounting_differs_from_payout')
        if action['op'] == 'claim' and success and after['recipient_balance'] - previous['recipient_balance'] != action['amount']:
            anomalies.append('successful_claim_paid_unexpected_amount')
        events.append(dict(id=f'tx-{i+1}', action=action, call_as=action.get('caller', 'self'),
                           succeeded=bool(success), fee=fee,
                           return_data=returned, before=previous, after=after, anomalies=anomalies))
        previous = after
    if list(iterator):
        raise ValueError('Unexpected trailing concrete observations')
    result = dict(execution='local_foundry_evm_calls', initial=initial, transactions=events,
                  source_sha256=hashlib.sha256(source.encode()).hexdigest(),
                  regressions_passed=regression_count, mined_transaction_hashes=False)
    (out / 'execution.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def compare(spec, observed):
    """Compare spec predictions and intent requirements to actual per-step state."""
    accounts = {p: dict(collected=0, earned=0, paid=0) for p in ('A', 'B')}
    custody = 0
    differences = []
    predictions = []
    for tx in observed['transactions']:
        a = tx['action']
        if a['op'] == 'call':
            after = tx['after']
            for pool in ('A', 'B'):
                for field in ('collected', 'earned', 'paid'):
                    accounts[pool][field] = after[pool + '_' + field]
            custody = after['custody']
            violations = []
            if any(after[pool + '_paid'] > after[pool + '_earned'] for pool in ('A', 'B')):
                violations.append('R2')
            if any(after[pool + '_paid'] > after[pool + '_collected'] for pool in ('A', 'B')):
                violations.append('R3')
            predictions.append(dict(id=tx['id'], unmodeled=True))
            if violations or tx.get('anomalies'):
                differences.append(dict(id=tx['id'], spec_mismatches=[], intent_violations=violations,
                                        execution_anomalies=tx.get('anomalies', [])))
            continue
        p = a['pool']; amount = a['amount']; state = accounts[p]
        if a['op'] == 'swap':
            fee = tx['fee']
            state['collected'] += fee
            state['earned'] += max(fee // 2, spec.get('minimum_rebate', 0)) if fee else 0
            custody += fee
            allowed = True
        else:
            owed = state['earned'] - state['paid'] if spec['claim_limit'] == 'cumulative' else state['earned']
            allowed = amount <= owed and amount <= custody
            if allowed:
                state['paid'] += amount; custody -= amount
        mismatches = []
        after = tx['after']
        if tx['succeeded'] != allowed:
            mismatches.append('call_outcome')
        for pool in ('A', 'B'):
            for field, value in accounts[pool].items():
                if after[pool + '_' + field] != value:
                    mismatches.append(pool + '_' + field)
        if after['custody'] != custody:
            mismatches.append('custody')
        violations = []
        if any(after[pool + '_paid'] > after[pool + '_earned'] for pool in ('A', 'B')):
            violations.append('R2')
        if any(after[pool + '_paid'] > after[pool + '_collected'] for pool in ('A', 'B')):
            violations.append('R3')
        prediction = dict(id=tx['id'], succeeded=allowed, accounts=copy.deepcopy(accounts), custody=custody)
        predictions.append(prediction)
        if mismatches or violations:
            differences.append(dict(id=tx['id'], spec_mismatches=mismatches, intent_violations=violations))
    return dict(predictions=predictions, findings=differences,
                claim='Finite concrete observations; fee inputs come from the EVM, not modeled AMM math.')
