"""
Gas 估算器

查询当前 Gas 价格，估算一笔交易的 Gas 成本。
套利决策时用它来判断：利润 - Gas 成本 > 0 才值得执行。

Optimism 的 Gas 特殊性：
- L2 执行 Gas 极低（通常 < 0.001 Gwei）
- 但还有 L1 数据费用（把交易数据提交到以太坊主网的成本）
- 总成本 = L2 执行费 + L1 数据费，通常合计 < $0.01

使用方式：
    estimator = GasEstimator(conn.w3, config.strategy)
    cost_usd = estimator.estimate_swap_cost_usd(eth_price_usd=3000)
"""

import logging
import time
from typing import Optional

from web3 import Web3

from utils.config import StrategyConfig

logger = logging.getLogger(__name__)

# 一笔 DEX swap 大约消耗的 Gas 单位
SWAP_GAS_ESTIMATE = 200_000
# 两笔 swap（套利 = 买+卖）
ARBITRAGE_GAS_ESTIMATE = SWAP_GAS_ESTIMATE * 2


class GasEstimator:
    """
    Gas 价格查询和交易成本估算。

    什么是 Gas？
    在区块链上执行交易需要"燃料费"，就像开车需要油费。
    Gas Price 越高，矿工/验证者越优先处理你的交易。

    Optimism 上 Gas 极低，一笔 swap 通常不到 $0.01。
    """

    # Gas 价格缓存 TTL（秒）。同一次评估周期内避免重复 RPC 调用。
    GAS_CACHE_TTL = 3.0

    def __init__(self, w3: Web3, strategy: StrategyConfig):
        self.w3 = w3
        self.strategy = strategy
        self._cached_gas_price: int = 0
        self._cache_time: float = 0.0

    def get_gas_price(self) -> int:
        """
        查询当前 Gas 价格（单位: Wei），带 TTL 缓存。

        缓存有效期内直接返回上次结果，避免同一次评估周期内
        重复 RPC 调用（一次套利评估可能调 3-4 次 get_gas_price）。
        """
        now = time.time()
        if self._cached_gas_price > 0 and (now - self._cache_time) < self.GAS_CACHE_TTL:
            return self._cached_gas_price

        try:
            gas_price = self.w3.eth.gas_price
            gas_price_gwei = gas_price / 1e9
            logger.debug("当前 Gas 价格: %.4f Gwei", gas_price_gwei)
            self._cached_gas_price = gas_price
            self._cache_time = now
            return gas_price
        except Exception as e:
            logger.error("获取 Gas 价格失败: %s", e)
            return self._cached_gas_price if self._cached_gas_price > 0 else 0

    def get_gas_price_gwei(self) -> float:
        """查询当前 Gas 价格（单位: Gwei，人类可读）"""
        return self.get_gas_price() / 1e9

    def is_gas_acceptable(self) -> bool:
        """
        判断当前 Gas 价格是否在可接受范围内。
        超过 max_gas_price 配置时返回 False，跳过本次交易。
        """
        gwei = self.get_gas_price_gwei()
        acceptable = gwei <= self.strategy.max_gas_price
        if not acceptable:
            logger.warning(
                "Gas 价格过高: %.4f Gwei > 上限 %.4f Gwei",
                gwei, self.strategy.max_gas_price,
            )
        return acceptable

    def estimate_swap_cost_eth(self) -> float:
        """
        估算一笔 swap 的 Gas 成本（单位: ETH）。

        计算公式：
        Gas 成本 = Gas Price × Gas Used
        """
        gas_price = self.get_gas_price()
        if gas_price == 0:
            return 0
        cost_wei = gas_price * SWAP_GAS_ESTIMATE
        return cost_wei / 1e18

    def estimate_arbitrage_cost_eth(self) -> float:
        """
        估算一次套利（两笔 swap）的 Gas 成本（单位: ETH）。
        """
        gas_price = self.get_gas_price()
        if gas_price == 0:
            return 0
        cost_wei = gas_price * ARBITRAGE_GAS_ESTIMATE
        return cost_wei / 1e18

    def estimate_arbitrage_cost_usd(self, eth_price_usd: float) -> float:
        """
        估算一次套利的 Gas 成本（单位: USD）。

        参数：
            eth_price_usd: 当前 ETH 价格（美元）

        示例：
            cost = estimator.estimate_arbitrage_cost_usd(eth_price_usd=3000)
            # → 0.005 (约 0.5 美分，Optimism 上 Gas 极低)
        """
        cost_eth = self.estimate_arbitrage_cost_eth()
        return cost_eth * eth_price_usd
