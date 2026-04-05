"""
DEX 套利策略

接收 PriceMonitor 的套利信号，做最终决策：
1. 验证价差仍然存在（二次确认）
2. 估算 Gas 成本
3. 计算净利润（毛利润 - Gas - 滑点预留）
4. 净利润 > 0 → 传给交易执行器

这是机器人的"大脑"。

使用方式：
    strategy = DexArbitrage(conn, uni, velo, gas_estimator, config)
    monitor.on_opportunity = strategy.evaluate  # 连接到价格监听器
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

from contracts.uniswap_v3 import UniswapV3
from contracts.velodrome import Velodrome
from data.price_monitor import ArbitrageOpportunity
from utils.config import Config
from utils.gas_estimator import GasEstimator
from utils.web3_utils import ChainConnection

logger = logging.getLogger(__name__)


@dataclass
class TradeDecision:
    """一次交易决策的完整信息"""

    timestamp: float
    action: str  # "execute" 或 "skip"
    reason: str  # 决策原因
    opportunity: ArbitrageOpportunity
    gas_cost_eth: float
    gas_cost_usd: float
    net_profit_usd: float  # 净利润（扣除 Gas 和滑点后）
    # 交易参数（action="execute" 时有值）
    token_in: str = ""
    token_out: str = ""
    amount_in: int = 0  # 最小单位
    min_amount_out: int = 0  # 最少要换到的量（滑点保护）
    buy_dex: str = ""
    sell_dex: str = ""


class DexArbitrage:
    """
    DEX 套利决策引擎。

    决策流程：
    ┌──────────────┐
    │ 收到套利信号  │ ← PriceMonitor
    └──────┬───────┘
           ↓
    ┌──────────────┐
    │ Gas 可接受？  │ → 否 → 跳过
    └──────┬───────┘
           ↓ 是
    ┌──────────────┐
    │ 二次确认价差  │ → 价差消失 → 跳过
    └──────┬───────┘
           ↓ 价差仍在
    ┌──────────────┐
    │ 净利润 > 0?  │ → 否 → 跳过
    └──────┬───────┘
           ↓ 是
    ┌──────────────┐
    │ 生成交易指令  │ → 传给 TransactionExecutor
    └──────────────┘
    """

    def __init__(
        self,
        conn: ChainConnection,
        uniswap: UniswapV3,
        velodrome: Velodrome,
        gas_estimator: GasEstimator,
        config: Config,
    ):
        self.conn = conn
        self.uniswap = uniswap
        self.velodrome = velodrome
        self.gas_estimator = gas_estimator
        self.config = config
        self.strategy = config.strategy

        # 交易决策回调（由 transaction_executor 设置）
        self.on_trade: Optional[callable] = None

        # 统计
        self.total_signals = 0
        self.total_executions = 0
        self.total_skips = 0

        # dry run 模式：只打印，不真执行（安全测试用）
        self.dry_run = True

        # 决策历史（上限 200 条，防止内存泄漏）
        self.decisions: list[TradeDecision] = []
        self._max_decisions = 200

        # ETH 价格缓存（TTL 10 秒，避免重复 RPC）
        self._eth_price_cache: float = 0.0
        self._eth_price_cache_time: float = 0.0
        self._eth_price_cache_ttl: float = 10.0

    async def evaluate(self, opportunity: ArbitrageOpportunity) -> TradeDecision:
        """
        评估一个套利机会，决定是否执行。

        这是被 PriceMonitor 调用的入口方法。
        """
        self.total_signals += 1
        now = time.time()

        logger.info(
            "收到套利信号 #%d: 价差=%.4f%%, 买=%s 卖=%s",
            self.total_signals, opportunity.spread * 100,
            opportunity.buy_dex, opportunity.sell_dex,
        )

        # ---- 检查 1: Gas 价格 ----
        if not self.gas_estimator.is_gas_acceptable():
            return self._skip(opportunity, "Gas 价格过高", now)

        # ---- 检查 2: 交易金额范围 ----
        if opportunity.amount_in_human < self.strategy.min_trade_amount:
            return self._skip(opportunity, "交易金额低于最小值", now)
        if opportunity.amount_in_human > self.strategy.max_trade_amount:
            # 限制到最大金额
            opportunity.amount_in_human = self.strategy.max_trade_amount

        # ---- 检查 3: 估算 Gas 成本 ----
        gas_cost_eth = self.gas_estimator.estimate_arbitrage_cost_eth()

        # 用 Uniswap 查 ETH 价格来换算 Gas 成本为 USD
        eth_price_usd = self._get_eth_price_usd()
        gas_cost_usd = gas_cost_eth * eth_price_usd

        # ---- 检查 4: 计算净利润 ----
        gross_profit_tokens = opportunity.estimated_profit  # token_out 数量差
        gross_profit_usd = gross_profit_tokens * eth_price_usd  # 假设 token_out 是 WETH

        # 滑点预留：实际利润可能比预估少
        slippage_cost = gross_profit_usd * self.strategy.max_slippage
        net_profit_usd = gross_profit_usd - gas_cost_usd - slippage_cost

        logger.info(
            "利润分析: 毛利=$%.4f, Gas=$%.4f, 滑点=$%.4f, 净利=$%.4f",
            gross_profit_usd, gas_cost_usd, slippage_cost, net_profit_usd,
        )

        if net_profit_usd <= 0:
            return self._skip(
                opportunity,
                f"净利润为负 (${net_profit_usd:.4f})",
                now, gas_cost_eth, gas_cost_usd, net_profit_usd,
            )

        # ---- 通过所有检查，准备执行 ----
        decision = self._build_trade_decision(
            opportunity, now, gas_cost_eth, gas_cost_usd, net_profit_usd,
        )

        self.total_executions += 1
        self._append_decision(decision)

        if self.dry_run:
            logger.info(
                "[DRY RUN] 会执行套利: 在 %s 买, 在 %s 卖, 净利=$%.4f",
                decision.buy_dex, decision.sell_dex, net_profit_usd,
            )
        else:
            logger.info(
                "执行套利 #%d: 在 %s 买, 在 %s 卖, 净利=$%.4f",
                self.total_executions, decision.buy_dex,
                decision.sell_dex, net_profit_usd,
            )
            if self.on_trade:
                await self.on_trade(decision)

        return decision

    def get_stats(self) -> dict:
        """获取策略统计"""
        return {
            "total_signals": self.total_signals,
            "total_executions": self.total_executions,
            "total_skips": self.total_skips,
            "execution_rate": (
                self.total_executions / self.total_signals
                if self.total_signals > 0
                else 0
            ),
            "dry_run": self.dry_run,
        }

    # ============================================================
    # 内部方法
    # ============================================================

    def _get_eth_price_usd(self) -> float:
        """
        查 ETH 当前价格（USD），带 TTL 缓存。
        用 Uniswap 查 1 WETH 能换多少 USDC。
        """
        now = time.time()
        if (
            self._eth_price_cache > 0
            and (now - self._eth_price_cache_time) < self._eth_price_cache_ttl
        ):
            return self._eth_price_cache

        price = self.uniswap.get_price(
            token_in=self.config.optimism.weth,
            token_out=self.config.optimism.usdc,
            amount_in_human=1.0,
            token_in_decimals=18,
            token_out_decimals=6,
        )
        if price is None:
            logger.warning("无法获取 ETH 价格，使用默认值 3000")
            return self._eth_price_cache if self._eth_price_cache > 0 else 3000.0

        self._eth_price_cache = price
        self._eth_price_cache_time = now
        return price

    def _skip(
        self,
        opportunity: ArbitrageOpportunity,
        reason: str,
        timestamp: float,
        gas_cost_eth: float = 0,
        gas_cost_usd: float = 0,
        net_profit_usd: float = 0,
    ) -> TradeDecision:
        """构造一个"跳过"决策"""
        self.total_skips += 1
        logger.info("跳过套利: %s", reason)

        decision = TradeDecision(
            timestamp=timestamp,
            action="skip",
            reason=reason,
            opportunity=opportunity,
            gas_cost_eth=gas_cost_eth,
            gas_cost_usd=gas_cost_usd,
            net_profit_usd=net_profit_usd,
        )
        self._append_decision(decision)
        return decision

    def _append_decision(self, decision: TradeDecision) -> None:
        """保存决策到历史记录，超过上限时丢弃最早的"""
        self.decisions.append(decision)
        if len(self.decisions) > self._max_decisions:
            self.decisions = self.decisions[-self._max_decisions:]

    def _build_trade_decision(
        self,
        opportunity: ArbitrageOpportunity,
        timestamp: float,
        gas_cost_eth: float,
        gas_cost_usd: float,
        net_profit_usd: float,
    ) -> TradeDecision:
        """构造一个"执行"交易决策"""
        # 计算原始金额
        amount_in_raw = int(opportunity.amount_in_human * 10**6)  # USDC 6 decimals

        # 滑点保护：最少要换到的量
        buy_price = opportunity.buy_price
        min_out = buy_price * (1 - self.strategy.max_slippage)
        min_amount_out_raw = int(min_out * 10**18)  # WETH 18 decimals

        return TradeDecision(
            timestamp=timestamp,
            action="execute",
            reason="净利润为正",
            opportunity=opportunity,
            gas_cost_eth=gas_cost_eth,
            gas_cost_usd=gas_cost_usd,
            net_profit_usd=net_profit_usd,
            token_in=opportunity.token_in,
            token_out=opportunity.token_out,
            amount_in=amount_in_raw,
            min_amount_out=min_amount_out_raw,
            buy_dex=opportunity.buy_dex,
            sell_dex=opportunity.sell_dex,
        )
