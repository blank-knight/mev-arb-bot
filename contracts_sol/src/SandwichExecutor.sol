// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "./interfaces/IERC20.sol";
import {ISwapRouter} from "./interfaces/ISwapRouter.sol";
import {IVelodromeRouter} from "./interfaces/IVelodromeRouter.sol";

/**
 * @title SandwichExecutor
 * @notice 三明治攻击执行合约：管理 frontrun 和 backrun 的资金和 swap
 *
 * 和 ArbitrageExecutor 的区别：
 * - 套利是一笔原子交易（买+卖），失败自动回滚
 * - 三明治是两笔独立交易（frontrun + backrun），时间上分开
 *
 * 工作原理：
 * 1. frontrun(): 在受害者之前买入 tokenOut（推高价格）
 * 2. （等待受害者交易上链）
 * 3. backrun(): 在受害者之后卖出 tokenOut（以更高价格卖出）
 *
 * 风险管理：
 * - backrun 有最小利润检查（防止亏损卖出）
 * - 紧急提取函数（万一 backrun 执行不了，可以手动取回资金）
 * - 只有 owner 能操作
 *
 * 资金流向：
 * frontrun: owner 钱包 → 合约 → DEX（买入 tokenOut，留在合约里）
 * backrun:  合约里的 tokenOut → DEX（卖出） → 合约 → owner 钱包
 */
contract SandwichExecutor {
    address public owner;

    address public uniswapRouter;
    address public velodromeRouter;
    address public velodromeFactory;

    // ---- 事件 ----
    event FrontrunExecuted(
        address indexed tokenIn,
        address indexed tokenOut,
        uint256 amountIn,
        uint256 amountOut,
        string dex
    );

    event BackrunExecuted(
        address indexed tokenIn,
        address indexed tokenOut,
        uint256 amountIn,
        uint256 amountOut,
        uint256 profit,
        string dex
    );

    event FundsWithdrawn(address indexed token, uint256 amount);

    // ---- 修饰符 ----
    modifier onlyOwner() {
        require(msg.sender == owner, "Not owner");
        _;
    }

    constructor(
        address _uniswapRouter,
        address _velodromeRouter,
        address _velodromeFactory
    ) {
        owner = msg.sender;
        uniswapRouter = _uniswapRouter;
        velodromeRouter = _velodromeRouter;
        velodromeFactory = _velodromeFactory;
    }

    // ============================================================
    // Frontrun：在受害者之前买入
    // ============================================================

    /**
     * @notice 在 Uniswap V3 上执行 frontrun 买入
     * @param tokenIn 输入代币（和受害者同方向）
     * @param tokenOut 输出代币
     * @param amountIn 买入金额
     * @param fee Uniswap V3 手续费等级
     * @return amountOut 实际买到的数量
     *
     * 执行后 tokenOut 留在合约里，等待 backrun 卖出。
     */
    function frontrunUniswap(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint24 fee
    ) external onlyOwner returns (uint256 amountOut) {
        // 从 owner 转入资金
        require(
            IERC20(tokenIn).transferFrom(msg.sender, address(this), amountIn),
            "TransferFrom failed"
        );

        // 在 Uniswap 买入
        amountOut = _swapOnUniswap(tokenIn, tokenOut, amountIn, fee);

        // tokenOut 留在合约里，等 backrun
        emit FrontrunExecuted(tokenIn, tokenOut, amountIn, amountOut, "uniswap");
    }

    /**
     * @notice 在 Velodrome 上执行 frontrun 买入
     */
    function frontrunVelodrome(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        bool stable
    ) external onlyOwner returns (uint256 amountOut) {
        require(
            IERC20(tokenIn).transferFrom(msg.sender, address(this), amountIn),
            "TransferFrom failed"
        );

        amountOut = _swapOnVelodrome(tokenIn, tokenOut, amountIn, stable);

        emit FrontrunExecuted(tokenIn, tokenOut, amountIn, amountOut, "velodrome");
    }

    // ============================================================
    // Backrun：在受害者之后卖出
    // ============================================================

    /**
     * @notice 在 Uniswap V3 上执行 backrun 卖出
     * @param tokenIn 要卖出的代币（frontrun 买到的）
     * @param tokenOut 要换回的代币
     * @param fee Uniswap V3 手续费等级
     * @param minProfit 最小利润要求（tokenOut 最小单位）
     *
     * 把合约里所有的 tokenIn 卖掉，换回 tokenOut，
     * 检查利润后全部转回给 owner。
     */
    function backrunUniswap(
        address tokenIn,
        address tokenOut,
        uint24 fee,
        uint256 minProfit,
        uint256 originalAmountIn
    ) external onlyOwner {
        uint256 balance = IERC20(tokenIn).balanceOf(address(this));
        require(balance > 0, "No balance to sell");

        // 卖出所有 tokenIn
        uint256 amountOut = _swapOnUniswap(tokenIn, tokenOut, balance, fee);

        // 检查利润（amountOut 应该 > 当初花的 originalAmountIn + minProfit）
        require(
            amountOut >= originalAmountIn + minProfit,
            "Profit too low"
        );

        uint256 profit = amountOut - originalAmountIn;

        // 全部转回给 owner
        require(
            IERC20(tokenOut).transfer(msg.sender, amountOut),
            "Transfer failed"
        );

        emit BackrunExecuted(
            tokenIn, tokenOut, balance, amountOut, profit, "uniswap"
        );
    }

    /**
     * @notice 在 Velodrome 上执行 backrun 卖出
     */
    function backrunVelodrome(
        address tokenIn,
        address tokenOut,
        bool stable,
        uint256 minProfit,
        uint256 originalAmountIn
    ) external onlyOwner {
        uint256 balance = IERC20(tokenIn).balanceOf(address(this));
        require(balance > 0, "No balance to sell");

        uint256 amountOut = _swapOnVelodrome(tokenIn, tokenOut, balance, stable);

        require(
            amountOut >= originalAmountIn + minProfit,
            "Profit too low"
        );

        uint256 profit = amountOut - originalAmountIn;

        require(
            IERC20(tokenOut).transfer(msg.sender, amountOut),
            "Transfer failed"
        );

        emit BackrunExecuted(
            tokenIn, tokenOut, balance, amountOut, profit, "velodrome"
        );
    }

    // ============================================================
    // 内部 swap 函数（和 ArbitrageExecutor 相同）
    // ============================================================

    function _swapOnUniswap(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint24 fee
    ) internal returns (uint256 amountOut) {
        IERC20(tokenIn).approve(uniswapRouter, amountIn);

        ISwapRouter.ExactInputSingleParams memory params = ISwapRouter
            .ExactInputSingleParams({
                tokenIn: tokenIn,
                tokenOut: tokenOut,
                fee: fee,
                recipient: address(this),
                deadline: block.timestamp + 300,
                amountIn: amountIn,
                amountOutMinimum: 0,
                sqrtPriceLimitX96: 0
            });

        amountOut = ISwapRouter(uniswapRouter).exactInputSingle(params);
    }

    function _swapOnVelodrome(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        bool stable
    ) internal returns (uint256 amountOut) {
        IERC20(tokenIn).approve(velodromeRouter, amountIn);

        IVelodromeRouter.Route[] memory routes = new IVelodromeRouter.Route[](1);
        routes[0] = IVelodromeRouter.Route({
            from: tokenIn,
            to: tokenOut,
            stable: stable,
            factory: velodromeFactory
        });

        uint256[] memory amounts = IVelodromeRouter(velodromeRouter)
            .swapExactTokensForTokens(
                amountIn,
                0,
                routes,
                address(this),
                block.timestamp + 300
            );

        amountOut = amounts[amounts.length - 1];
    }

    // ============================================================
    // 管理函数
    // ============================================================

    /**
     * @notice 紧急提取代币（万一 backrun 无法执行时使用）
     */
    function withdrawToken(address token) external onlyOwner {
        uint256 balance = IERC20(token).balanceOf(address(this));
        require(balance > 0, "No balance");
        require(IERC20(token).transfer(owner, balance), "Transfer failed");
        emit FundsWithdrawn(token, balance);
    }

    /**
     * @notice 提取 ETH
     */
    function withdrawETH() external onlyOwner {
        uint256 balance = address(this).balance;
        require(balance > 0, "No balance");
        (bool success, ) = payable(owner).call{value: balance}("");
        require(success, "ETH transfer failed");
    }

    /**
     * @notice 更新 DEX Router 地址
     */
    function updateRouters(
        address _uniswapRouter,
        address _velodromeRouter,
        address _velodromeFactory
    ) external onlyOwner {
        uniswapRouter = _uniswapRouter;
        velodromeRouter = _velodromeRouter;
        velodromeFactory = _velodromeFactory;
    }

    receive() external payable {}
}
