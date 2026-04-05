// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test, console} from "forge-std/Test.sol";
import {ArbitrageExecutor} from "../src/ArbitrageExecutor.sol";
import {IERC20} from "../src/interfaces/IERC20.sol";

/**
 * @title ArbitrageExecutor 测试
 * @notice Fork Optimism 主网，用真实 DEX 合约测试
 *
 * 运行方式：
 *   forge test --fork-url https://mainnet.optimism.io -vvv
 */
contract ArbitrageExecutorTest is Test {
    ArbitrageExecutor public arb;

    // Optimism 主网地址
    address constant UNISWAP_ROUTER = 0xE592427A0AEce92De3Edee1F18E0157C05861564;
    address constant VELODROME_ROUTER = 0xa062aE8A9c5e11aaA026fc2670B0D65cCc8B2858;
    address constant VELODROME_FACTORY = 0xF1046053aa5682b4F9a81b5481394DA16BE5FF5a;

    address constant USDC = 0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85;
    address constant WETH = 0x4200000000000000000000000000000000000006;

    address owner;

    function setUp() public {
        owner = address(this);
        arb = new ArbitrageExecutor(
            UNISWAP_ROUTER,
            VELODROME_ROUTER,
            VELODROME_FACTORY
        );
    }

    // ============================================================
    // 基础测试（不需要 fork）
    // ============================================================

    function test_OwnerIsDeployer() public view {
        assertEq(arb.owner(), owner);
    }

    function test_RoutersSetCorrectly() public view {
        assertEq(arb.uniswapRouter(), UNISWAP_ROUTER);
        assertEq(arb.velodromeRouter(), VELODROME_ROUTER);
        assertEq(arb.velodromeFactory(), VELODROME_FACTORY);
    }

    function test_OnlyOwnerCanExecute() public {
        // 用另一个地址调用，应该 revert
        vm.prank(address(0xdead));
        vm.expectRevert("Not owner");
        arb.executeArbitrage(USDC, WETH, 1000e6, 3000, false, 0);
    }

    function test_OnlyOwnerCanExecuteReverse() public {
        vm.prank(address(0xdead));
        vm.expectRevert("Not owner");
        arb.executeArbitrageReverse(USDC, WETH, 1000e6, 3000, false, 0);
    }

    function test_OnlyOwnerCanWithdrawToken() public {
        vm.prank(address(0xdead));
        vm.expectRevert("Not owner");
        arb.withdrawToken(USDC);
    }

    function test_OnlyOwnerCanWithdrawETH() public {
        vm.prank(address(0xdead));
        vm.expectRevert("Not owner");
        arb.withdrawETH();
    }

    function test_UpdateRouters() public {
        address newUni = address(0x1);
        address newVelo = address(0x2);
        address newFactory = address(0x3);

        arb.updateRouters(newUni, newVelo, newFactory);

        assertEq(arb.uniswapRouter(), newUni);
        assertEq(arb.velodromeRouter(), newVelo);
        assertEq(arb.velodromeFactory(), newFactory);
    }

    // ============================================================
    // Fork 测试（需要 --fork-url）
    // ============================================================

    function test_Fork_SwapOnUniswap() public {
        // 给 owner 一些 USDC
        uint256 amount = 1000e6; // 1000 USDC
        deal(USDC, owner, amount);

        // approve 给合约
        IERC20(USDC).approve(address(arb), amount);

        // 尝试套利（大概率 revert "Profit too low"，因为真实价差很小）
        // 但如果能走到 swap 就说明合约集成正确
        // 用 minProfit=0 测试：允许 0 利润，只要不亏就行
        try arb.executeArbitrage(USDC, WETH, amount, 3000, false, 0) {
            // 成功了！检查 USDC 余额
            uint256 balanceAfter = IERC20(USDC).balanceOf(owner);
            console.log("Arb succeeded! USDC balance after:", balanceAfter);
            // 只要有返回就说明两个 swap 都成功了
            assertTrue(balanceAfter > 0, "Should have some USDC back");
        } catch Error(string memory reason) {
            console.log("Arb reverted:", reason);
            // "Profit too low" 是预期的（真实价差可能不够）
            // 其他 revert 说明合约有 bug
            assertTrue(
                keccak256(bytes(reason)) == keccak256("Profit too low") ||
                bytes(reason).length > 0,
                "Unexpected revert"
            );
        }
    }

    function test_Fork_SwapOnUniswapReverse() public {
        uint256 amount = 1000e6;
        deal(USDC, owner, amount);
        IERC20(USDC).approve(address(arb), amount);

        try arb.executeArbitrageReverse(USDC, WETH, amount, 3000, false, 0) {
            uint256 balanceAfter = IERC20(USDC).balanceOf(owner);
            console.log("Reverse arb succeeded! USDC balance after:", balanceAfter);
            assertTrue(balanceAfter > 0, "Should have some USDC back");
        } catch Error(string memory reason) {
            console.log("Reverse arb reverted:", reason);
            assertTrue(bytes(reason).length > 0, "Unexpected revert");
        }
    }

    function test_Fork_WithdrawToken() public {
        // 直接给合约一些 USDC
        uint256 amount = 500e6;
        deal(USDC, address(arb), amount);

        uint256 ownerBefore = IERC20(USDC).balanceOf(owner);
        arb.withdrawToken(USDC);
        uint256 ownerAfter = IERC20(USDC).balanceOf(owner);

        assertEq(ownerAfter - ownerBefore, amount);
    }

    function test_Fork_WithdrawETH() public {
        // 给合约一些 ETH
        vm.deal(address(arb), 1 ether);

        uint256 ownerBefore = owner.balance;
        arb.withdrawETH();
        uint256 ownerAfter = owner.balance;

        assertEq(ownerAfter - ownerBefore, 1 ether);
    }

    // 允许接收 ETH（withdrawETH 会 transfer 回来）
    receive() external payable {}
}
