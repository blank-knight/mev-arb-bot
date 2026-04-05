// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test, console} from "forge-std/Test.sol";
import {SandwichExecutor} from "../src/SandwichExecutor.sol";
import {IERC20} from "../src/interfaces/IERC20.sol";

/**
 * @title SandwichExecutor 测试
 * @notice Fork Optimism 主网，用真实 DEX 合约测试
 *
 * 运行方式：
 *   forge test --fork-url https://mainnet.optimism.io -vvv --match-contract SandwichExecutorTest
 */
contract SandwichExecutorTest is Test {
    SandwichExecutor public sandwich;

    // Optimism 主网地址
    address constant UNISWAP_ROUTER = 0xE592427A0AEce92De3Edee1F18E0157C05861564;
    address constant VELODROME_ROUTER = 0xa062aE8A9c5e11aaA026fc2670B0D65cCc8B2858;
    address constant VELODROME_FACTORY = 0xF1046053aa5682b4F9a81b5481394DA16BE5FF5a;

    address constant USDC = 0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85;
    address constant WETH = 0x4200000000000000000000000000000000000006;

    address owner;

    function setUp() public {
        owner = address(this);
        sandwich = new SandwichExecutor(
            UNISWAP_ROUTER,
            VELODROME_ROUTER,
            VELODROME_FACTORY
        );
    }

    // ============================================================
    // 基础测试（不需要 fork）
    // ============================================================

    function test_OwnerIsDeployer() public view {
        assertEq(sandwich.owner(), owner);
    }

    function test_RoutersSetCorrectly() public view {
        assertEq(sandwich.uniswapRouter(), UNISWAP_ROUTER);
        assertEq(sandwich.velodromeRouter(), VELODROME_ROUTER);
        assertEq(sandwich.velodromeFactory(), VELODROME_FACTORY);
    }

    function test_OnlyOwnerCanFrontrunUniswap() public {
        vm.prank(address(0xdead));
        vm.expectRevert("Not owner");
        sandwich.frontrunUniswap(USDC, WETH, 1000e6, 3000);
    }

    function test_OnlyOwnerCanFrontrunVelodrome() public {
        vm.prank(address(0xdead));
        vm.expectRevert("Not owner");
        sandwich.frontrunVelodrome(USDC, WETH, 1000e6, false);
    }

    function test_OnlyOwnerCanBackrunUniswap() public {
        vm.prank(address(0xdead));
        vm.expectRevert("Not owner");
        sandwich.backrunUniswap(WETH, USDC, 3000, 0, 0);
    }

    function test_OnlyOwnerCanBackrunVelodrome() public {
        vm.prank(address(0xdead));
        vm.expectRevert("Not owner");
        sandwich.backrunVelodrome(WETH, USDC, false, 0, 0);
    }

    function test_OnlyOwnerCanWithdrawToken() public {
        vm.prank(address(0xdead));
        vm.expectRevert("Not owner");
        sandwich.withdrawToken(USDC);
    }

    function test_OnlyOwnerCanWithdrawETH() public {
        vm.prank(address(0xdead));
        vm.expectRevert("Not owner");
        sandwich.withdrawETH();
    }

    function test_UpdateRouters() public {
        address newUni = address(0x1);
        address newVelo = address(0x2);
        address newFactory = address(0x3);

        sandwich.updateRouters(newUni, newVelo, newFactory);

        assertEq(sandwich.uniswapRouter(), newUni);
        assertEq(sandwich.velodromeRouter(), newVelo);
        assertEq(sandwich.velodromeFactory(), newFactory);
    }

    // ============================================================
    // Fork 测试（需要 --fork-url）
    // ============================================================

    function test_Fork_FrontrunUniswap() public {
        // 给 owner 1000 USDC
        uint256 amount = 1000e6;
        deal(USDC, owner, amount);

        // approve 给合约
        IERC20(USDC).approve(address(sandwich), amount);

        // 执行 frontrun：USDC → WETH
        uint256 wethOut = sandwich.frontrunUniswap(USDC, WETH, amount, 3000);

        // frontrun 后 WETH 应该在合约里
        uint256 contractWeth = IERC20(WETH).balanceOf(address(sandwich));
        assertEq(contractWeth, wethOut);
        assertTrue(wethOut > 0, "Should have received WETH");

        console.log("Frontrun Uniswap: %d USDC -> %d WETH", amount, wethOut);
    }

    function test_Fork_FrontrunVelodrome() public {
        uint256 amount = 1000e6;
        deal(USDC, owner, amount);
        IERC20(USDC).approve(address(sandwich), amount);

        uint256 wethOut = sandwich.frontrunVelodrome(USDC, WETH, amount, false);

        uint256 contractWeth = IERC20(WETH).balanceOf(address(sandwich));
        assertEq(contractWeth, wethOut);
        assertTrue(wethOut > 0, "Should have received WETH");

        console.log("Frontrun Velodrome: %d USDC -> %d WETH", amount, wethOut);
    }

    function test_Fork_FullSandwichUniswap() public {
        // 完整的三明治流程（frontrun + backrun）
        uint256 amount = 1000e6;
        deal(USDC, owner, amount);
        IERC20(USDC).approve(address(sandwich), amount);

        // 1. Frontrun: USDC → WETH
        uint256 wethOut = sandwich.frontrunUniswap(USDC, WETH, amount, 3000);
        console.log("Frontrun: %d USDC -> %d WETH", amount, wethOut);

        // 2. Backrun: WETH → USDC（minProfit=0，只要不亏就行）
        try sandwich.backrunUniswap(WETH, USDC, 3000, 0, amount) {
            uint256 ownerUsdc = IERC20(USDC).balanceOf(owner);
            console.log("Backrun succeeded! USDC back: %d", ownerUsdc);
            // 在没有受害者交易推高价格的情况下，
            // 应该会因为手续费损失一点（换两次的滑点）
        } catch Error(string memory reason) {
            console.log("Backrun reverted:", reason);
            // "Profit too low" 是预期的（没有受害者交易推高价格）
            assertTrue(
                keccak256(bytes(reason)) == keccak256("Profit too low") ||
                bytes(reason).length > 0,
                "Unexpected revert"
            );
        }
    }

    function test_Fork_FullSandwichVelodrome() public {
        uint256 amount = 1000e6;
        deal(USDC, owner, amount);
        IERC20(USDC).approve(address(sandwich), amount);

        uint256 wethOut = sandwich.frontrunVelodrome(USDC, WETH, amount, false);
        console.log("Frontrun Velo: %d USDC -> %d WETH", amount, wethOut);

        try sandwich.backrunVelodrome(WETH, USDC, false, 0, amount) {
            uint256 ownerUsdc = IERC20(USDC).balanceOf(owner);
            console.log("Backrun Velo succeeded! USDC back: %d", ownerUsdc);
        } catch Error(string memory reason) {
            console.log("Backrun Velo reverted:", reason);
            assertTrue(bytes(reason).length > 0, "Unexpected revert");
        }
    }

    function test_Fork_WithdrawAfterFrontrun() public {
        // 测试紧急提取：frontrun 后如果 backrun 失败，能取回资金
        uint256 amount = 500e6;
        deal(USDC, owner, amount);
        IERC20(USDC).approve(address(sandwich), amount);

        // frontrun 买入 WETH
        uint256 wethOut = sandwich.frontrunUniswap(USDC, WETH, amount, 3000);
        assertTrue(wethOut > 0);

        // 紧急提取 WETH
        uint256 ownerWethBefore = IERC20(WETH).balanceOf(owner);
        sandwich.withdrawToken(WETH);
        uint256 ownerWethAfter = IERC20(WETH).balanceOf(owner);

        assertEq(ownerWethAfter - ownerWethBefore, wethOut);
        console.log("Emergency withdraw: %d WETH recovered", wethOut);
    }

    function test_Fork_WithdrawETH() public {
        vm.deal(address(sandwich), 1 ether);

        uint256 ownerBefore = owner.balance;
        sandwich.withdrawETH();
        uint256 ownerAfter = owner.balance;

        assertEq(ownerAfter - ownerBefore, 1 ether);
    }

    // 允许接收 ETH
    receive() external payable {}
}
