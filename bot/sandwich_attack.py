"""
三明治攻击策略

什么是三明治攻击？
    想象你在排队买东西，有人看到你要买很多某个商品（会推高价格），
    于是他在你前面先买了一些（frontrun），等你的大单把价格推高后，
    他再以更高的价格卖出（backrun）。差价就是他的利润。

    在 DEX 上：
    1. 受害者发出一笔大额 swap（还在 mempool 里，没上链）
    2. 我们看到后，发送 frontrun 交易：同方向买入（推高价格）
    3. 受害者的交易以更高价格成交（他多付了钱）
    4. 我们发送 backrun 交易：反方向卖出（以更高价格卖掉）
    5. 我们的利润 = 卖出价 - 买入价 - Gas 费

关键区别（vs 套利）：
    - 套利：一笔原子交易（买+卖），失败只亏 Gas
    - 三明治：两笔独立交易（frontrun + backrun），有更多风险：
      * 受害者交易可能不上链（我们的 frontrun 就白做了）
      * 另一个 MEV bot 可能抢在我们前面
      * 价格影响估算不准可能导致亏损

Optimism 上的特殊性：
    - 单一 sequencer，FIFO 排序（先到先处理）
    - 没有 L1 那样的 Gas 竞价机制
    - 区块时间 2 秒，窗口很短
    - Gas 费极低（< $0.01），降低了三明治的最低利润门槛

使用方式：
    sandwich = SandwichStrategy(conn, uni, velo, gas_estimator, config)
    mempool_monitor.on_large_swap = sandwich.evaluate
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

from contracts.uniswap_v3 import UniswapV3
from contracts.velodrome import Velodrome
from data.mempool_monitor import PendingSwap
from utils.config import Config
from utils.gas_estimator import GasEstimator
from utils.web3_utils import ChainConnection

logger = logging.getLogger(__name__)


@dataclass
class SandwichOpportunity:
    """
    一次三明治攻击机会的分析结果。

    包含了前置交易和后置交易的所有参数。
    """

    timestamp: float

    # 受害者交易信息
    victim_tx_hash: str
    victim_dex: str
    victim_token_in: str
    victim_token_out: str
    victim_amount_in: int
    victim_amount_in_human: float

    # 前置交易参数（frontrun）
    frontrun_token_in: str      # 和受害者同方向买入
    frontrun_token_out: str
    frontrun_amount_in: int
    frontrun_amount_in_human: float
    frontrun_expected_out: int   # 预期换到的数量

    # 后置交易参数（backrun）
    backrun_token_in: str       # 把 frontrun 买到的卖回去
    backrun_token_out: str
    backrun_expected_out: int    # 预期换回的数量

    # 利润分析
    price_impact: float          # 受害者交易的价格影响（百分比）
    estimated_profit: float      # 预估利润（token_in 最小单位）
    estimated_profit_usd: float  # 预估利润（USD）
    gas_cost_usd: float          # Gas 成本（两笔交易）
    net_profit_usd: float        # 净利润


@dataclass
class SandwichDecision:
    """三明治攻击决策"""

    timestamp: float
    action: str  # "execute" 或 "skip"
    reason: str
    opportunity: Optional[SandwichOpportunity] = None


class SandwichStrategy:
    """
    三明治攻击决策引擎。

    决策流程：
    ┌─────────────────────────┐
    │ 收到大额 swap 信号       │ ← MempoolMonitor
    └──────────┬──────────────┘
               ↓
    ┌─────────────────────────┐
    │ Gas 可接受？             │ → 否 → 跳过
    └──────────┬──────────────┘
               ↓ 是
    ┌─────────────────────────┐
    │ 计算价格影响             │ → 影响太小 → 跳过
    └──────────┬──────────────┘
               ↓
    ┌─────────────────────────┐
    │ 模拟 frontrun 利润       │
    └──────────┬──────────────┘
               ↓
    ┌─────────────────────────┐
    │ 净利润 > 0?             │ → 否 → 跳过
    └──────────┬──────────────┘
               ↓ 是
    ┌─────────────────────────┐
    │ 生成 frontrun + backrun  │ → 传给执行器
    └─────────────────────────┘
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

        # frontrun 金额占受害者交易的比例
        # 太高会被检测到；太低利润不够
        self.frontrun_ratio = 0.3  # 用受害者金额的 30%

        # 最小价格影响阈值（低于此值利润太薄）
        self.min_price_impact = 0.001  # 0.1%

        # 执行回调
        self.on_frontrun: Optional[callable] = None
        self.on_backrun: Optional[callable] = None

        # dry run 模式（默认开启）
        self.dry_run = True

        # 统计
        self.total_signals = 0
        self.total_executions = 0
        self.total_skips = 0
        self.decisions: list[SandwichDecision] = []
        self._max_decisions = 200

        # ETH 价格缓存（TTL 10 秒）
        self._eth_price_cache: float = 0.0
        self._eth_price_cache_time: float = 0.0
        self._eth_price_cache_ttl: float = 10.0

    async def evaluate(self, pending_swap: PendingSwap) -> SandwichDecision:
        """
        评估一笔 pending swap 是否值得三明治攻击。

        这是被 MempoolMonitor 调用的入口方法。
        """
        self.total_signals += 1
        now = time.time()

        logger.info(
            "评估三明治 #%d: DEX=%s, 金额=%.2f, tx=%s",
            self.total_signals, pending_swap.dex,
            pending_swap.amount_in_human, pending_swap.tx_hash[:18],
        )

        # ---- 检查 1: Gas 价格 ----
        if not self.gas_estimator.is_gas_acceptable():
            return self._skip("Gas 价格过高", now)

        # ---- 检查 2: 估算受害者交易的价格影响 ----
        price_impact = self._estimate_price_impact(pending_swap)
        if price_impact is None:
            return self._skip("无法估算价格影响", now)

        if price_impact < self.min_price_impact:
            return self._skip(
                f"价格影响太小 ({price_impact:.4%} < {self.min_price_impact:.4%})",
                now,
            )

        # ---- 检查 3: 计算 frontrun 参数和预期利润 ----
        opportunity = self._calculate_opportunity(
            pending_swap, price_impact, now
        )
        if opportunity is None:
            return self._skip("无法计算利润", now)

        # ---- 检查 4: 净利润 > 0？----
        if opportunity.net_profit_usd <= 0:
            return self._skip(
                f"净利润为负 (${opportunity.net_profit_usd:.4f})",
                now, opportunity,
            )

        # ---- 通过所有检查，准备执行 ----
        self.total_executions += 1
        decision = SandwichDecision(
            timestamp=now,
            action="execute",
            reason="净利润为正",
            opportunity=opportunity,
        )
        self._append_decision(decision)

        if self.dry_run:
            logger.info(
                "[DRY RUN] 三明治 #%d: 价格影响=%.4f%%, "
                "frontrun=%.2f, 净利=$%.4f",
                self.total_executions, price_impact * 100,
                opportunity.frontrun_amount_in_human,
                opportunity.net_profit_usd,
            )
        else:
            logger.info(
                "执行三明治 #%d: 价格影响=%.4f%%, "
                "frontrun=%.2f, 净利=$%.4f",
                self.total_executions, price_impact * 100,
                opportunity.frontrun_amount_in_human,
                opportunity.net_profit_usd,
            )
            if self.on_frontrun:
                await self.on_frontrun(opportunity)

        return decision

    def _estimate_price_impact(self, swap: PendingSwap) -> Optional[float]:
        """
        估算受害者交易对价格的影响。

        方法：
        1. 查当前报价（用小额度）
        2. 查受害者的大额度报价
        3. 比较两个报价的差异

        价格影响 = (小额单价 - 大额单价) / 小额单价

        例如：
        - 1 USDC → 0.000500 WETH（小额）
        - 10000 USDC → 0.004990 WETH（大额，单价更差）
        - 价格影响 = (0.000500 - 0.000499) / 0.000500 = 0.2%

        价格影响越大，三明治攻击的利润空间越大。
        """
        try:
            # 确定使用哪个 DEX 查价
            if swap.dex == "uniswap":
                dex = self.uniswap
            else:
                dex = self.velodrome

            token_in_decimals = self._get_decimals(swap.token_in)
            token_out_decimals = self._get_decimals(swap.token_out)

            # 小额查价（基准价格）
            small_amount = 1.0  # 1 个 token
            small_price = dex.get_price(
                swap.token_in, swap.token_out,
                small_amount,
                token_in_decimals, token_out_decimals,
            )

            # 大额查价（受害者的交易量）
            large_price = dex.get_price(
                swap.token_in, swap.token_out,
                swap.amount_in_human,
                token_in_decimals, token_out_decimals,
            )

            if small_price is None or large_price is None:
                return None
            if small_price == 0:
                return None

            # 换算为单位价格
            small_unit_price = small_price / small_amount
            large_unit_price = large_price / swap.amount_in_human

            # 价格影响 = 大单比小单单价差多少
            impact = (small_unit_price - large_unit_price) / small_unit_price

            logger.debug(
                "价格影响估算: 小额单价=%.8f, 大额单价=%.8f, 影响=%.4f%%",
                small_unit_price, large_unit_price, impact * 100,
            )
            return abs(impact)

        except Exception as e:
            logger.error("价格影响估算失败: %s", e)
            return None

    def _calculate_opportunity(
        self,
        swap: PendingSwap,
        price_impact: float,
        timestamp: float,
    ) -> Optional[SandwichOpportunity]:
        """
        计算三明治攻击的完整参数和预期利润。

        核心思路：
        1. 我们用 frontrun_ratio * 受害者金额 做 frontrun（同方向买入）
        2. 受害者交易推高价格
        3. 我们把买到的东西卖回去（backrun）
        4. 利润 ≈ frontrun_amount * price_impact（简化估算）

        为什么利润 ≈ frontrun_amount * price_impact？
        - 我们以当前价格买入
        - 受害者交易推高了 price_impact 的价格
        - 我们以更高的价格卖出
        - 价差 ≈ price_impact
        """
        try:
            token_in_decimals = self._get_decimals(swap.token_in)
            token_out_decimals = self._get_decimals(swap.token_out)

            # frontrun 金额
            frontrun_amount_human = swap.amount_in_human * self.frontrun_ratio

            # 限制在策略配置的范围内
            frontrun_amount_human = min(
                frontrun_amount_human,
                self.strategy.max_trade_amount,
            )
            frontrun_amount_human = max(
                frontrun_amount_human,
                self.strategy.min_trade_amount,
            )

            frontrun_amount_raw = int(
                frontrun_amount_human * (10 ** token_in_decimals)
            )

            # 模拟 frontrun：查当前报价
            if swap.dex == "uniswap":
                frontrun_out = self.uniswap.get_quote(
                    swap.token_in, swap.token_out,
                    frontrun_amount_raw,
                )
            else:
                frontrun_out = self.velodrome.get_quote(
                    swap.token_in, swap.token_out,
                    frontrun_amount_raw,
                )

            if frontrun_out is None or frontrun_out == 0:
                return None

            # 模拟 backrun：用同一个 DEX 把买到的卖回去
            # 注意：这里的报价不包含受害者交易的价格影响，
            # 实际 backrun 时价格会更高（对我们有利），所以这是保守估算
            if swap.dex == "uniswap":
                backrun_out = self.uniswap.get_quote(
                    swap.token_out, swap.token_in,
                    frontrun_out,
                )
            else:
                backrun_out = self.velodrome.get_quote(
                    swap.token_out, swap.token_in,
                    frontrun_out,
                )

            if backrun_out is None:
                return None

            # 利润 = backrun 换回的 - frontrun 花出去的
            # 这是保守估算，因为 backrun 时价格比现在高
            # 实际利润 ≈ profit_conservative + frontrun_amount * price_impact
            profit_conservative = backrun_out - frontrun_amount_raw
            profit_from_impact = frontrun_amount_raw * price_impact

            # 取两者中间值作为预估
            estimated_profit = profit_conservative + int(profit_from_impact * 0.5)

            # 转换为 USD
            estimated_profit_human = estimated_profit / (10 ** token_in_decimals)

            # 如果 token_in 不是稳定币，需要换算
            if token_in_decimals == 18:
                # token_in 是 WETH 或类似 18 decimals 的代币
                eth_price = self._get_eth_price_usd()
                estimated_profit_usd = estimated_profit_human * eth_price
            else:
                # 假设是稳定币（USDC/USDT, 6 decimals）
                estimated_profit_usd = estimated_profit_human

            # Gas 成本（两笔交易）
            gas_cost_eth = self.gas_estimator.estimate_swap_cost_eth() * 2
            eth_price = self._get_eth_price_usd()
            gas_cost_usd = gas_cost_eth * eth_price

            net_profit_usd = estimated_profit_usd - gas_cost_usd

            logger.info(
                "三明治利润分析: 保守利润=%.4f, 影响利润=%.4f, "
                "Gas=$%.4f, 净利=$%.4f",
                profit_conservative / (10 ** token_in_decimals),
                profit_from_impact / (10 ** token_in_decimals),
                gas_cost_usd, net_profit_usd,
            )

            return SandwichOpportunity(
                timestamp=timestamp,
                victim_tx_hash=swap.tx_hash,
                victim_dex=swap.dex,
                victim_token_in=swap.token_in,
                victim_token_out=swap.token_out,
                victim_amount_in=swap.amount_in,
                victim_amount_in_human=swap.amount_in_human,
                frontrun_token_in=swap.token_in,
                frontrun_token_out=swap.token_out,
                frontrun_amount_in=frontrun_amount_raw,
                frontrun_amount_in_human=frontrun_amount_human,
                frontrun_expected_out=frontrun_out,
                backrun_token_in=swap.token_out,
                backrun_token_out=swap.token_in,
                backrun_expected_out=backrun_out,
                price_impact=price_impact,
                estimated_profit=estimated_profit,
                estimated_profit_usd=estimated_profit_usd,
                gas_cost_usd=gas_cost_usd,
                net_profit_usd=net_profit_usd,
            )

        except Exception as e:
            logger.error("三明治利润计算失败: %s", e)
            return None

    def _skip(
        self,
        reason: str,
        timestamp: float,
        opportunity: Optional[SandwichOpportunity] = None,
    ) -> SandwichDecision:
        """构造一个"跳过"决策"""
        self.total_skips += 1
        logger.info("跳过三明治: %s", reason)

        decision = SandwichDecision(
            timestamp=timestamp,
            action="skip",
            reason=reason,
            opportunity=opportunity,
        )
        self._append_decision(decision)
        return decision

    def _append_decision(self, decision: SandwichDecision) -> None:
        """保存决策到历史记录，超过上限时丢弃最早的"""
        self.decisions.append(decision)
        if len(self.decisions) > self._max_decisions:
            self.decisions = self.decisions[-self._max_decisions:]

    def _get_decimals(self, token_addr: str) -> int:
        """
        获取代币精度。

        已知代币直接返回，未知代币默认 18。
        （生产环境应该从合约读取 decimals()，这里简化处理）
        """
        known = {
            self.config.optimism.usdc.lower(): 6,
            self.config.optimism.usdt.lower(): 6,
            self.config.optimism.weth.lower(): 18,
            self.config.optimism.op.lower(): 18,
        }
        return known.get(token_addr.lower(), 18)

    def _get_eth_price_usd(self) -> float:
        """查 ETH/USD 价格，带 TTL 缓存"""
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
