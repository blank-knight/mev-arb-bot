"""
Backrun 策略（Optimism 适配版）

为什么在 Optimism 上不做 frontrun？
    Optimism 使用单一 Sequencer，交易按到达顺序（FIFO）排列，
    不像 L1 那样按 Gas Price 排序。出更高的 Gas 无法插到别人前面。

    "Op上不是谁最快，是谁更会跟" —— 正确策略是 backrun（跟跑）。

Backrun 套利的核心逻辑：
    1. 监听 Mempool 中的大额 swap（受害者）
    2. 预测受害者 swap 执行后的跨 DEX 价差：
       - 受害者在 Uniswap 买了大量 WETH → Uniswap 的 WETH 价格上涨
       - Velodrome 的 WETH 价格还没变
       - 价差 = 机会
    3. 立刻提交跟随套利：在 Velodrome 买入 WETH，在 Uniswap（价格已升）卖出
    4. 本质上是"由 Mempool 信号触发的 DEX 套利"

与主动 DEX 套利的区别：
    - 主动套利：轮询价差，等待机会被动出现（周期性）
    - Backrun：预测大单将创造的机会，主动跟随（事件驱动）
    → Backrun 能捕捉到大单后短暂出现、很快消失的价差窗口

关键参数说明：
    - backrun_ratio: 我们投入金额占受害者金额的比例（默认 30%）
      太高会占用过多资金；太低利润不足覆盖 Gas
    - min_predicted_spread: 最小预测价差阈值（默认 0.2%）
      比轮询套利阈值略高，因为预测本身有误差

使用方式：
    sandwich = SandwichStrategy(conn, uni, velo, gas_estimator, config)
    sandwich.on_backrun = executor.execute  # 连接执行器
    mempool_monitor.on_large_swap = sandwich.evaluate
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

from bot.dex_arbitrage import TradeDecision
from contracts.uniswap_v3 import UniswapV3
from contracts.velodrome import Velodrome
from data.mempool_monitor import PendingSwap
from data.price_monitor import ArbitrageOpportunity
from utils.config import Config
from utils.gas_estimator import GasEstimator
from utils.web3_utils import ChainConnection

logger = logging.getLogger(__name__)


@dataclass
class SandwichOpportunity:
    """
    Backrun 机会分析结果。

    包含受害者 swap 信息、预测价差，以及我们将执行的 backrun 参数。
    """

    timestamp: float

    # 受害者信息
    victim_tx_hash: str
    victim_dex: str
    victim_token_in: str
    victim_token_out: str
    victim_amount_in: int
    victim_amount_in_human: float

    # 预测分析
    predicted_spread: float       # 受害者执行后预测的跨 DEX 价差
    victim_implied_rate: float    # 受害者隐含执行价（tokenIn/tokenOut 人类单位）
    other_dex_rate: float         # 另一个 DEX 当前价（tokenIn/tokenOut 人类单位）

    # 我们的 backrun 参数
    buy_dex: str                  # 在哪里买入 tokenOut
    sell_dex: str                 # 在哪里卖出 tokenOut
    our_amount_in_human: float    # 我们投入的金额

    # 利润分析
    estimated_profit_usd: float
    gas_cost_usd: float
    net_profit_usd: float


@dataclass
class SandwichDecision:
    """Backrun 决策结果（含统计信息和实际执行指令）"""

    timestamp: float
    action: str  # "execute" 或 "skip"
    reason: str
    opportunity: Optional[SandwichOpportunity] = None
    trade_decision: Optional[TradeDecision] = None  # 实际传给 Executor 的指令


class SandwichStrategy:
    """
    Backrun 策略引擎（Optimism 适配版）。

    只做 backrun，不做 frontrun。

    决策流程：
    ┌─────────────────────────┐
    │ 收到大额 pending swap    │ ← MempoolMonitor
    └──────────┬──────────────┘
               ↓
    ┌─────────────────────────┐
    │ Gas 可接受？             │ → 否 → 跳过
    └──────────┬──────────────┘
               ↓ 是
    ┌─────────────────────────┐
    │ 预测执行后跨 DEX 价差    │ → 价差太小 → 跳过
    └──────────┬──────────────┘
               ↓ 价差够大
    ┌─────────────────────────┐
    │ 计算净利润               │ → 净利润 ≤ 0 → 跳过
    └──────────┬──────────────┘
               ↓ 有利润
    ┌─────────────────────────┐
    │ 生成 TradeDecision       │ → on_backrun → TransactionExecutor
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

        # Backrun 金额占受害者金额的比例
        # 30% 是平衡"利润空间"和"占用资金"的经验值
        self.backrun_ratio = 0.3

        # 最小预测价差阈值
        # 设为 0.2%（略高于轮询套利的 0.3% min_profit_threshold，因为预测有误差）
        self.min_predicted_spread = 0.002

        # Backrun 执行回调（在 BotManager 中设置为 executor.execute）
        self.on_backrun: Optional[callable] = None

        # dry run 模式（默认开启，安全第一）
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
        评估一笔 pending swap 是否值得 backrun。

        这是被 MempoolMonitor 调用的入口方法。
        """
        self.total_signals += 1
        now = time.time()

        logger.info(
            "评估 Backrun #%d: DEX=%s, 金额=%.2f, tx=%s",
            self.total_signals, pending_swap.dex,
            pending_swap.amount_in_human, pending_swap.tx_hash[:18],
        )

        # ---- 检查 1: Gas 价格 ----
        if not self.gas_estimator.is_gas_acceptable():
            return self._skip("Gas 价格过高", now)

        # ---- 检查 2: 预测受害者执行后的跨 DEX 价差 ----
        spread_info = self._predict_post_swap_spread(pending_swap)
        if spread_info is None:
            return self._skip("无法预测价差", now)

        predicted_spread, buy_dex, sell_dex, our_amount_in_raw, victim_rate, other_rate = spread_info

        logger.info(
            "预测价差: %.4f%%, buy=%s, sell=%s",
            predicted_spread * 100, buy_dex, sell_dex,
        )

        if predicted_spread < self.min_predicted_spread:
            return self._skip(
                f"预测价差太小 ({predicted_spread:.4%} < {self.min_predicted_spread:.4%})",
                now,
            )

        # ---- 检查 3: 计算净利润 ----
        token_in_decimals = self._get_decimals(pending_swap.token_in)
        our_amount_human = our_amount_in_raw / 10 ** token_in_decimals

        eth_price_usd = self._get_eth_price_usd()

        # 估算利润 = 投入金额 × 预测价差
        # token_in 是 USDC（稳定币）时直接用美元；是 WETH 时换算
        if token_in_decimals == 18:
            estimated_profit_usd = our_amount_human * predicted_spread * eth_price_usd
        else:
            estimated_profit_usd = our_amount_human * predicted_spread

        gas_cost_usd = self.gas_estimator.estimate_arbitrage_cost_eth() * eth_price_usd
        net_profit_usd = estimated_profit_usd - gas_cost_usd

        if net_profit_usd <= 0:
            return self._skip(
                f"净利润为负 (${net_profit_usd:.4f})",
                now,
            )

        # ---- 构造 backrun 机会记录 ----
        opportunity = SandwichOpportunity(
            timestamp=now,
            victim_tx_hash=pending_swap.tx_hash,
            victim_dex=pending_swap.dex,
            victim_token_in=pending_swap.token_in,
            victim_token_out=pending_swap.token_out,
            victim_amount_in=pending_swap.amount_in,
            victim_amount_in_human=pending_swap.amount_in_human,
            predicted_spread=predicted_spread,
            victim_implied_rate=victim_rate,
            other_dex_rate=other_rate,
            buy_dex=buy_dex,
            sell_dex=sell_dex,
            our_amount_in_human=our_amount_human,
            estimated_profit_usd=estimated_profit_usd,
            gas_cost_usd=gas_cost_usd,
            net_profit_usd=net_profit_usd,
        )

        # ---- 构造 TradeDecision（和 DexArbitrage 一样的结构，交给 Executor 执行）----
        # Backrun 本质上就是一笔 DEX 套利，只是触发来源是 Mempool 信号
        arb_opp = ArbitrageOpportunity(
            timestamp=now,
            token_in=pending_swap.token_in,
            token_out=pending_swap.token_out,
            amount_in_human=our_amount_human,
            buy_dex=buy_dex,
            sell_dex=sell_dex,
            buy_price=1.0 / victim_rate if victim_rate > 0 else 0,
            sell_price=1.0 / other_rate if other_rate > 0 else 0,
            spread=predicted_spread,
            estimated_profit=0,
        )

        trade_decision = TradeDecision(
            timestamp=now,
            action="execute",
            reason=f"Backrun: victim={pending_swap.tx_hash[:10]}, 预测价差={predicted_spread:.4%}",
            opportunity=arb_opp,
            gas_cost_eth=self.gas_estimator.estimate_arbitrage_cost_eth(),
            gas_cost_usd=gas_cost_usd,
            net_profit_usd=net_profit_usd,
            token_in=pending_swap.token_in,
            token_out=pending_swap.token_out,
            amount_in=our_amount_in_raw,
            min_amount_out=0,  # 链上合约负责利润检查
            buy_dex=buy_dex,
            sell_dex=sell_dex,
        )

        self.total_executions += 1
        decision = SandwichDecision(
            timestamp=now,
            action="execute",
            reason="净利润为正",
            opportunity=opportunity,
            trade_decision=trade_decision,
        )
        self._append_decision(decision)

        if self.dry_run:
            logger.info(
                "[DRY RUN] Backrun #%d: victim=%s, 预测价差=%.4f%%, 净利=$%.4f",
                self.total_executions, pending_swap.tx_hash[:10],
                predicted_spread * 100, net_profit_usd,
            )
        else:
            logger.info(
                "执行 Backrun #%d: victim=%s, 预测价差=%.4f%%, 净利=$%.4f",
                self.total_executions, pending_swap.tx_hash[:10],
                predicted_spread * 100, net_profit_usd,
            )
            if self.on_backrun:
                await self.on_backrun(trade_decision)

        return decision

    # ============================================================
    # 核心逻辑：预测受害者执行后的跨 DEX 价差
    # ============================================================

    def _predict_post_swap_spread(
        self, swap: PendingSwap
    ) -> Optional[tuple[float, str, str, int, float, float]]:
        """
        预测受害者 swap 执行后的跨 DEX 价差。

        核心思路（以受害者在 Uniswap 买 WETH 为例）：

        1. 调用 uniswap.get_quote(USDC, WETH, victim_amount) → victim_weth_out
           这个 quote 模拟了受害者的完整交易，反映了受害者的平均执行价格：
           victim_implied_rate = victim_amount_usdc / victim_weth_out

        2. 查 Velodrome 当前价格（还没被受害者影响）：
           velodrome_rate = 1 / velodrome.get_price(USDC, WETH, 1.0)

        3. 如果 victim_implied_rate > velodrome_rate：
           说明受害者把 Uniswap 上的 WETH 价格推高了
           → 我们应该：在 Velodrome 买 WETH（便宜），在 Uniswap 卖 WETH（贵）

        注意：victim_implied_rate 是均价（不是边际价格），实际机会略大于预测。
        这是保守估算，用于过滤明显无利润的情况。

        返回：(spread, buy_dex, sell_dex, our_amount_in_raw, victim_rate, other_rate)
        或 None（无法计算时）
        """
        try:
            victim_dex_obj = self.uniswap if swap.dex == "uniswap" else self.velodrome
            other_dex_obj = self.velodrome if swap.dex == "uniswap" else self.uniswap
            other_dex_name = "velodrome" if swap.dex == "uniswap" else "uniswap"

            token_in_decimals = self._get_decimals(swap.token_in)
            token_out_decimals = self._get_decimals(swap.token_out)

            # Step 1: 受害者 DEX 上的执行 quote（模拟受害者的完整交易）
            victim_amount_out_raw = victim_dex_obj.get_quote(
                swap.token_in, swap.token_out, swap.amount_in
            )
            if not victim_amount_out_raw or victim_amount_out_raw == 0:
                logger.debug("无法获取受害者 DEX quote")
                return None

            # 受害者的隐含执行价格（人类单位：tokenIn per tokenOut）
            # 例如：10000 USDC / 4.7 WETH = 2128 USDC/WETH
            victim_in_human = swap.amount_in / 10 ** token_in_decimals
            victim_out_human = victim_amount_out_raw / 10 ** token_out_decimals
            if victim_out_human == 0:
                return None
            victim_rate = victim_in_human / victim_out_human  # tokenIn per tokenOut

            # Step 2: 另一个 DEX 当前价格
            # get_price(tokenIn, tokenOut, 1.0) → amount_out_human（tokenOut per 1 tokenIn）
            other_price = other_dex_obj.get_price(
                swap.token_in, swap.token_out,
                1.0, token_in_decimals, token_out_decimals,
            )
            if not other_price or other_price == 0:
                logger.debug("无法获取另一 DEX 价格")
                return None

            other_rate = 1.0 / other_price  # tokenIn per tokenOut（与 victim_rate 单位一致）

            # Step 3: 计算价差和方向
            if victim_rate > other_rate:
                # 受害者推高了 victim_dex 上 tokenOut 的价格
                # → tokenOut 在 victim_dex 更贵，在 other_dex 更便宜
                # → 在 other_dex 买入 tokenOut，在 victim_dex 卖出
                spread = (victim_rate - other_rate) / other_rate
                buy_dex = other_dex_name
                sell_dex = swap.dex
            else:
                # 反向情况（受害者在 victim_dex 卖出 tokenIn，使 tokenOut 变便宜）
                spread = (other_rate - victim_rate) / victim_rate
                buy_dex = swap.dex
                sell_dex = other_dex_name

            # Step 4: 计算我们的投入金额
            our_amount_human = min(
                swap.amount_in_human * self.backrun_ratio,
                self.strategy.max_trade_amount,
            )
            our_amount_human = max(our_amount_human, self.strategy.min_trade_amount)
            our_amount_in_raw = int(our_amount_human * 10 ** token_in_decimals)

            logger.debug(
                "Backrun 预测: victim_rate=%.4f, other_rate=%.4f, "
                "spread=%.4f%%, buy=%s, sell=%s, amount=%.2f",
                victim_rate, other_rate, spread * 100,
                buy_dex, sell_dex, our_amount_human,
            )

            return spread, buy_dex, sell_dex, our_amount_in_raw, victim_rate, other_rate

        except Exception as e:
            logger.error("预测价差失败: %s", e)
            return None

    # ============================================================
    # 辅助方法
    # ============================================================

    def _skip(
        self,
        reason: str,
        timestamp: float,
        opportunity: Optional[SandwichOpportunity] = None,
    ) -> SandwichDecision:
        """构造一个"跳过"决策"""
        self.total_skips += 1
        logger.info("跳过 Backrun: %s", reason)

        decision = SandwichDecision(
            timestamp=timestamp,
            action="skip",
            reason=reason,
            opportunity=opportunity,
        )
        self._append_decision(decision)
        return decision

    def _append_decision(self, decision: SandwichDecision) -> None:
        """保存决策记录，超过上限时丢弃最早的"""
        self.decisions.append(decision)
        if len(self.decisions) > self._max_decisions:
            self.decisions = self.decisions[-self._max_decisions:]

    def _get_decimals(self, token_addr: str) -> int:
        """获取代币精度，未知代币默认 18"""
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
