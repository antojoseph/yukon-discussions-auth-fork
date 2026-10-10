// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {BaseTestHooks} from "v4-core/src/test/BaseTestHooks.sol";
import {IHooks} from "v4-core/src/interfaces/IHooks.sol";
import {IPoolManager} from "v4-core/src/interfaces/IPoolManager.sol";
import {PoolKey} from "v4-core/src/types/PoolKey.sol";
import {PoolId, PoolIdLibrary} from "v4-core/src/types/PoolId.sol";
import {SwapParams} from "v4-core/src/types/PoolOperation.sol";
import {BalanceDelta} from "v4-core/src/types/BalanceDelta.sol";
import {Currency} from "v4-core/src/types/Currency.sol";

/// @notice Local research fixture. Seeded defect belongs to this hook, not Uniswap.
/// @dev Exact-input ERC20 swaps through the dedicated router only. No admin or upgrades.
contract RebateHook is BaseTestHooks {
    using PoolIdLibrary for PoolKey;

    IPoolManager public immutable manager;
    address public immutable router;
    bool public immutable repaired;
    uint256 public constant FEE_BPS = 100; // 1% of gross output, rounded down

    mapping(PoolId => mapping(Currency => uint256)) public collected;
    mapping(PoolId => mapping(Currency => uint256)) public paid;
    mapping(PoolId => mapping(Currency => mapping(address => uint256))) public earned;
    mapping(PoolId => mapping(Currency => mapping(address => uint256))) public claimed;

    error OnlyManager();
    error OnlyRouter();
    error UnsupportedSwap();
    error InvalidClaim();

    constructor(IPoolManager manager_, address router_, bool repaired_) {
        manager = manager_;
        router = router_;
        repaired = repaired_;
    }

    function afterSwap(address sender, PoolKey calldata key, SwapParams calldata params,
        BalanceDelta delta, bytes calldata hookData) external override returns (bytes4, int128)
    {
        if (msg.sender != address(manager)) revert OnlyManager();
        if (sender != router) revert OnlyRouter();
        if (params.amountSpecified >= 0) revert UnsupportedSwap();
        int128 output = params.zeroForOne ? delta.amount1() : delta.amount0();
        if (output < 0) revert UnsupportedSwap();
        Currency currency = params.zeroForOne ? key.currency1 : key.currency0;
        if (Currency.unwrap(currency) == address(0)) revert UnsupportedSwap();
        uint256 fee = uint256(uint128(output)) * FEE_BPS / 10_000;
        address recipient = abi.decode(hookData, (address)); // dedicated router supplies msg.sender
        PoolId pool = key.toId();
        collected[pool][currency] += fee;
        earned[pool][currency][recipient] += fee / 2; // 50% rebate, rounded down per swap
        manager.take(currency, address(this), fee);
        return (IHooks.afterSwap.selector, int128(int256(fee)));
    }

    function claim(PoolId pool, Currency currency, uint256 amount) external {
        uint256 entitlement = earned[pool][currency][msg.sender];
        if (amount == 0) revert InvalidClaim();
        // Proposed repair: cumulative entitlement and pool-local backing.
        if (amount > entitlement - claimed[pool][currency][msg.sender]) revert InvalidClaim();
        if (amount > collected[pool][currency] - paid[pool][currency]) revert InvalidClaim();
        claimed[pool][currency][msg.sender] += amount;
        paid[pool][currency] += amount;
        currency.transfer(msg.sender, amount); // effects first; failed transfers revert accounting
    }
}
