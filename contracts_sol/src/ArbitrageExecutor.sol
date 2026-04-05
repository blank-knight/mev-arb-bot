// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "./interfaces/IERC20.sol";
import {ISwapRouter} from "./interfaces/ISwapRouter.sol";
import {IVelodromeRouter} from "./interfaces/IVelodromeRouter.sol";

/**
 * @title ArbitrageExecutor
 * @notice 原子套利合约：在一笔交易内完成 买入+卖出，失败自动回滚
 *
 * 工作原理：
 * 1. 从你的钱包转入 tokenIn（如 USDC）
 * 2. 在便宜的 DEX 买入 tokenOut（如 WETH）
 * 3. 在贵的 DEX 卖出 tokenOut 换回 tokenIn
 * 4. 检查：最终 tokenIn 余额 > 初始余额 + minProfit
 * 5. 不满足 → 整笔交易 revert，只损失 Gas
 *
 * 安全设计：
 * - 只有 owner 能调用（防止别人用你的合约）
 * - 利润检查在链上完成（不依赖链下计算）
 * - 失败自动回滚，最坏情况只损失 Gas 费
 */
contract ArbitrageExecutor {
    address public owner;

    // Optimism 上的 DEX Router 地址
    address public uniswapRouter;
    address public velodromeRouter;
    address public velodromeFactory;

    // ---- 事件 ----
    event ArbitrageExecuted(
        address indexed tokenIn,
        address indexed tokenOut,
        uint256 amountIn,
        uint256 profit,
        string buyDex,
        string sellDex
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

    /**
     * @notice 执行套利：在 Velodrome 买，在 Uniswap 卖
     * @param tokenIn 输入代币（如 USDC）
     * @param tokenOut 输出代币（如 WETH）
     * @param amountIn 输入数量（最小单位）
     * @param uniswapFee Uniswap V3 手续费等级（如 3000 = 0.3%）
     * @param veloStable Velodrome 是否用稳定池
     * @param minProfit 最小利润要求（tokenIn 最小单位）
     */
    function executeArbitrage(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint24 uniswapFee,
        bool veloStable,
        uint256 minProfit
    ) external onlyOwner {
        // 1. 从调用者转入 tokenIn
        require(
            IERC20(tokenIn).transferFrom(msg.sender, address(this), amountIn),
            "TransferFrom failed"
        );

        // 记录初始 tokenIn 余额
        uint256 balanceBefore = IERC20(tokenIn).balanceOf(address(this));

        // 2. 在 Velodrome 买入：tokenIn → tokenOut
        uint256 amountOut = _swapOnVelodrome(
            tokenIn, tokenOut, amountIn, veloStable
        );

        // 3. 在 Uniswap 卖出：tokenOut → tokenIn
        _swapOnUniswap(tokenOut, tokenIn, amountOut, uniswapFee);

        // 4. 检查利润
        uint256 balanceAfter = IERC20(tokenIn).balanceOf(address(this));
        require(
            balanceAfter >= balanceBefore + minProfit,
            "Profit too low"
        );

        uint256 profit = balanceAfter - balanceBefore;

        // 5. 把利润 + 本金转回给 owner
        require(
            IERC20(tokenIn).transfer(msg.sender, balanceAfter),
            "Transfer failed"
        );

        emit ArbitrageExecuted(
            tokenIn, tokenOut, amountIn, profit,
            "velodrome", "uniswap"
        );
    }

    /**
     * @notice 反向套利：在 Uniswap 买，在 Velodrome 卖
     */
    function executeArbitrageReverse(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint24 uniswapFee,
        bool veloStable,
        uint256 minProfit
    ) external onlyOwner {
        require(
            IERC20(tokenIn).transferFrom(msg.sender, address(this), amountIn),
            "TransferFrom failed"
        );
        uint256 balanceBefore = IERC20(tokenIn).balanceOf(address(this));

        // 在 Uniswap 买入
        uint256 amountOut = _swapOnUniswap(
            tokenIn, tokenOut, amountIn, uniswapFee
        );

        // 在 Velodrome 卖出
        _swapOnVelodrome(tokenOut, tokenIn, amountOut, veloStable);

        uint256 balanceAfter = IERC20(tokenIn).balanceOf(address(this));
        require(balanceAfter >= balanceBefore + minProfit, "Profit too low");

        uint256 profit = balanceAfter - balanceBefore;
        require(
            IERC20(tokenIn).transfer(msg.sender, balanceAfter),
            "Transfer failed"
        );

        emit ArbitrageExecuted(
            tokenIn, tokenOut, amountIn, profit,
            "uniswap", "velodrome"
        );
    }

    // ============================================================
    // 内部 swap 函数
    // ============================================================

    function _swapOnUniswap(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint24 fee
    ) internal returns (uint256 amountOut) {
        // 授权 Router 花费 tokenIn
        IERC20(tokenIn).approve(uniswapRouter, amountIn);

        ISwapRouter.ExactInputSingleParams memory params = ISwapRouter
            .ExactInputSingleParams({
                tokenIn: tokenIn,
                tokenOut: tokenOut,
                fee: fee,
                recipient: address(this),
                deadline: block.timestamp + 300, // 5 分钟过期
                amountIn: amountIn,
                amountOutMinimum: 0, // 利润检查在外层做
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
                0, // 利润检查在外层做
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
     * @notice 提取合约里残留的代币（应急用）
     */
    function withdrawToken(address token) external onlyOwner {
        uint256 balance = IERC20(token).balanceOf(address(this));
        require(balance > 0, "No balance");
        require(IERC20(token).transfer(owner, balance), "Transfer failed");
        emit FundsWithdrawn(token, balance);
    }

    /**
     * @notice 提取合约里残留的 ETH（应急用）
     */
    function withdrawETH() external onlyOwner {
        uint256 balance = address(this).balance;
        require(balance > 0, "No balance");
        (bool success, ) = payable(owner).call{value: balance}("");
        require(success, "ETH transfer failed");
    }

    /**
     * @notice 更新 DEX Router 地址（升级用）
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

    // 允许接收 ETH
    receive() external payable {}
}
