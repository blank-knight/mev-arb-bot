// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Script, console} from "forge-std/Script.sol";
import {ArbitrageExecutor} from "../src/ArbitrageExecutor.sol";
import {SandwichExecutor} from "../src/SandwichExecutor.sol";

/**
 * @title Deploy
 * @notice 部署套利合约和三明治合约到 Optimism
 *
 * 使用方式:
 *
 *   # 测试网部署 (Optimism Sepolia)
 *   forge script script/Deploy.s.sol:Deploy \
 *     --rpc-url $OPTIMISM_RPC_HTTP \
 *     --private-key $PRIVATE_KEY \
 *     --broadcast \
 *     --verify
 *
 *   # 主网部署 (Optimism Mainnet)
 *   forge script script/Deploy.s.sol:Deploy \
 *     --rpc-url $OPTIMISM_RPC_HTTP \
 *     --private-key $PRIVATE_KEY \
 *     --broadcast \
 *     --verify \
 *     --etherscan-api-key $OPTIMISTIC_ETHERSCAN_API_KEY
 *
 *   # 只模拟，不真部署
 *   forge script script/Deploy.s.sol:Deploy \
 *     --rpc-url $OPTIMISM_RPC_HTTP \
 *     --private-key $PRIVATE_KEY
 */
contract Deploy is Script {
    // Optimism Mainnet DEX 地址
    address constant UNISWAP_V3_ROUTER = 0xE592427A0AEce92De3Edee1F18E0157C05861564;
    address constant VELODROME_ROUTER = 0xa062aE8A9c5e11aaA026fc2670B0D65cCc8B2858;
    address constant VELODROME_FACTORY = 0xF1046053aa5682b4F9a81b5481394DA16BE5FF5a;

    function run() external {
        uint256 deployerKey = vm.envUint("PRIVATE_KEY");

        vm.startBroadcast(deployerKey);

        // 1. 部署套利合约
        ArbitrageExecutor arb = new ArbitrageExecutor(
            UNISWAP_V3_ROUTER,
            VELODROME_ROUTER,
            VELODROME_FACTORY
        );
        console.log("ArbitrageExecutor deployed at:", address(arb));

        // 2. 部署三明治合约
        SandwichExecutor sandwich = new SandwichExecutor(
            UNISWAP_V3_ROUTER,
            VELODROME_ROUTER,
            VELODROME_FACTORY
        );
        console.log("SandwichExecutor deployed at:", address(sandwich));

        vm.stopBroadcast();

        // 3. 打印部署摘要
        console.log("");
        console.log("========== Deployment Summary ==========");
        console.log("Chain ID:            ", block.chainid);
        console.log("ArbitrageExecutor:   ", address(arb));
        console.log("SandwichExecutor:    ", address(sandwich));
        console.log("");
        console.log("Next steps:");
        console.log("1. Copy ArbitrageExecutor address to .env ARBITRAGE_CONTRACT");
        console.log("2. Approve USDC/WETH to ArbitrageExecutor");
        console.log("3. Run: python main.py --poll");
        console.log("=========================================");
    }
}
