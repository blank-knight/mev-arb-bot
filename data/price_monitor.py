"""
价格监听器（WebSocket 事件驱动）

核心逻辑：
1. 订阅 Uniswap V3 和 Velodrome Pool 的 Swap 事件
2. 有人在任意一个 DEX 交易时 → 立刻查两边实时价格
3. 计算价差 → 超过阈值 → 触发套利回调

这是整个套利机器人的"耳朵"。

使用方式：
    monitor = PriceMonitor(conn, uni, velo, config)
    monitor.on_opportunity = my_callback  # 设置套利回调
    await monitor.start()
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Coroutine, Optional

from web3 import Web3

from contracts.uniswap_v3 import UniswapV3
from contracts.velodrome import Velodrome
from utils.config import Config, OptimismConfig, StrategyConfig
from utils.web3_utils import ChainConnection

logger = logging.getLogger(__name__)


@dataclass
class PriceSnapshot:
    """一次价格快照，记录两个 DEX 的报价和价差"""

    timestamp: float
    token_in: str
    token_out: str
    amount_in_human: float
    uniswap_price: Optional[float]  # Uniswap 报价（token_out 数量）
    velodrome_price: Optional[float]  # Velodrome 报价
    spread: float = 0.0  # 价差百分比
    buy_on: str = ""  # 在哪个 DEX 买入（便宜的那个）
    sell_on: str = ""  # 在哪个 DEX 卖出（贵的那个）


@dataclass
class ArbitrageOpportunity:
    """一次套利机会的完整信息"""

    timestamp: float
    token_in: str
    token_out: str
    amount_in_human: float
    buy_dex: str  # "uniswap" 或 "velodrome"
    sell_dex: str
    buy_price: float  # 在买入 DEX 的报价
    sell_price: float  # 在卖出 DEX 的报价
    spread: float  # 价差百分比
    estimated_profit: float  # 预估毛利润（token_out 数量）


class PriceMonitor:
    """
    价格监听器：监听两个 DEX 的 Swap 事件，发现价差就通知策略层。

    工作方式：
    1. 启动时，找到两个 DEX 的 Pool 地址
    2. 通过 WebSocket 订阅 Swap 事件
    3. 收到事件 → 查两边价格 → 计算价差
    4. 价差 > 阈值 → 调用 on_opportunity 回调

    也支持轮询模式（作为 WebSocket 的降级方案）。
    """

    def __init__(
        self,
        conn: ChainConnection,
        uniswap: UniswapV3,
        velodrome: Velodrome,
        config: Config,
    ):
        self.conn = conn
        self.uniswap = uniswap
        self.velodrome = velodrome
        self.config = config
        self.strategy = config.strategy

        # 套利机会回调（由 dex_arbitrage.py 设置）
        self.on_opportunity: Optional[
            Callable[[ArbitrageOpportunity], Coroutine]
        ] = None

        # Pool 地址缓存
        self._uni_pool: Optional[str] = None
        self._velo_pool: Optional[str] = None

        # 价格历史（最近 100 条）
        self.price_history: list[PriceSnapshot] = []
        self._max_history = 100

        # 防抖：同一秒内不重复查价
        self._last_check_time: float = 0
        self._min_check_interval: float = 0.5  # 最短间隔 0.5 秒

        # 监控状态
        self._running = False
        self._event_count = 0

    async def start(
        self,
        token_in: Optional[str] = None,
        token_out: Optional[str] = None,
        amount_in_human: float = 1000.0,
        token_in_decimals: int = 6,
        token_out_decimals: int = 18,
        uni_fee: int = 3000,
        velo_stable: bool = False,
    ) -> None:
        """
        启动价格监听。

        参数：
            token_in: 输入代币地址（默认 USDC）
            token_out: 输出代币地址（默认 WETH）
            amount_in_human: 模拟交易金额（默认 1000 USDC）
            token_in_decimals: 输入代币精度
            token_out_decimals: 输出代币精度
            uni_fee: Uniswap V3 手续费等级
            velo_stable: Velodrome 是否用稳定池
        """
        token_in = token_in or self.config.optimism.usdc
        token_out = token_out or self.config.optimism.weth

        logger.info(
            "启动价格监听: %s → %s, 金额=%s, 阈值=%.2f%%",
            token_in[:10], token_out[:10],
            amount_in_human,
            self.strategy.min_profit_threshold * 100,
        )

        # 1. 找到两个 DEX 的 Pool 地址
        self._uni_pool = self.uniswap.get_pool(token_in, token_out, uni_fee)
        self._velo_pool = self.velodrome.get_pool(token_in, token_out, velo_stable)

        if not self._uni_pool and not self._velo_pool:
            logger.error("两个 DEX 都没有找到池子，无法监听")
            return

        logger.info("Uniswap V3 Pool: %s", self._uni_pool or "未找到")
        logger.info("Velodrome Pool:  %s", self._velo_pool or "未找到")

        # 2. 先查一次当前价格（启动时的基准）
        await self._check_prices(
            token_in, token_out, amount_in_human,
            token_in_decimals, token_out_decimals,
            uni_fee, velo_stable,
        )

        # 3. 尝试 WebSocket 订阅 Swap 事件
        self._running = True
        ws_success = await self._subscribe_swap_events(
            token_in, token_out, amount_in_human,
            token_in_decimals, token_out_decimals,
            uni_fee, velo_stable,
        )

        if ws_success:
            logger.info("WebSocket 事件订阅成功，等待 Swap 事件...")
        else:
            # 降级为轮询模式
            logger.warning("WebSocket 订阅失败，降级为轮询模式 (5 秒间隔)")
            asyncio.create_task(self._polling_loop(
                token_in, token_out, amount_in_human,
                token_in_decimals, token_out_decimals,
                uni_fee, velo_stable,
            ))

    async def stop(self) -> None:
        """停止价格监听"""
        self._running = False
        logger.info("价格监听已停止 (共收到 %d 个事件)", self._event_count)

    async def check_once(
        self,
        token_in: Optional[str] = None,
        token_out: Optional[str] = None,
        amount_in_human: float = 1000.0,
        token_in_decimals: int = 6,
        token_out_decimals: int = 18,
        uni_fee: int = 3000,
        velo_stable: bool = False,
    ) -> Optional[PriceSnapshot]:
        """
        手动查一次价格（不用 WebSocket，直接查）。
        用于测试和手动检查。
        """
        token_in = token_in or self.config.optimism.usdc
        token_out = token_out or self.config.optimism.weth

        return await self._check_prices(
            token_in, token_out, amount_in_human,
            token_in_decimals, token_out_decimals,
            uni_fee, velo_stable,
        )

    # ============================================================
    # 核心价格查询逻辑
    # ============================================================

    async def _check_prices(
        self,
        token_in: str,
        token_out: str,
        amount_in_human: float,
        token_in_decimals: int,
        token_out_decimals: int,
        uni_fee: int,
        velo_stable: bool,
    ) -> Optional[PriceSnapshot]:
        """
        查询两边价格并计算价差。

        这是核心方法：
        1. 调 Uniswap Quoter 查价
        2. 调 Velodrome Router 查价
        3. 比较价差
        4. 超过阈值 → 构造 ArbitrageOpportunity 并回调
        """
        # 防抖
        now = time.time()
        if now - self._last_check_time < self._min_check_interval:
            return None
        self._last_check_time = now

        # 查两边价格（都是只读调用，不花 Gas）
        uni_price = self.uniswap.get_price(
            token_in, token_out, amount_in_human,
            token_in_decimals, token_out_decimals, uni_fee,
        )
        velo_price = self.velodrome.get_price(
            token_in, token_out, amount_in_human,
            token_in_decimals, token_out_decimals, velo_stable,
        )

        # 构造快照
        snapshot = PriceSnapshot(
            timestamp=now,
            token_in=token_in,
            token_out=token_out,
            amount_in_human=amount_in_human,
            uniswap_price=uni_price,
            velodrome_price=velo_price,
        )

        # 至少一个有报价才能比较
        if uni_price is None or velo_price is None:
            logger.warning(
                "价格查询不完整: Uniswap=%s, Velodrome=%s",
                uni_price, velo_price,
            )
            self._save_snapshot(snapshot)
            return snapshot

        # 计算价差
        if uni_price > velo_price:
            # Uniswap 报价更高 → 在 Velodrome 买（便宜），在 Uniswap 卖（贵）
            spread = (uni_price - velo_price) / velo_price
            snapshot.buy_on = "velodrome"
            snapshot.sell_on = "uniswap"
        else:
            # Velodrome 报价更高
            spread = (velo_price - uni_price) / uni_price
            snapshot.buy_on = "uniswap"
            snapshot.sell_on = "velodrome"

        snapshot.spread = spread

        logger.info(
            "价格 | Uni=%.6f | Velo=%.6f | 价差=%.4f%% | 买=%s 卖=%s",
            uni_price, velo_price, spread * 100,
            snapshot.buy_on, snapshot.sell_on,
        )

        self._save_snapshot(snapshot)

        # 价差超过阈值 → 通知策略层
        if spread >= self.strategy.min_profit_threshold:
            buy_price = velo_price if snapshot.buy_on == "velodrome" else uni_price
            sell_price = uni_price if snapshot.sell_on == "uniswap" else velo_price

            opportunity = ArbitrageOpportunity(
                timestamp=now,
                token_in=token_in,
                token_out=token_out,
                amount_in_human=amount_in_human,
                buy_dex=snapshot.buy_on,
                sell_dex=snapshot.sell_on,
                buy_price=buy_price,
                sell_price=sell_price,
                spread=spread,
                estimated_profit=sell_price - buy_price,
            )

            logger.info(
                "🎯 套利机会! 价差=%.4f%% > 阈值=%.4f%%, 预估利润=%.6f",
                spread * 100,
                self.strategy.min_profit_threshold * 100,
                opportunity.estimated_profit,
            )

            if self.on_opportunity:
                await self.on_opportunity(opportunity)

        return snapshot

    # ============================================================
    # WebSocket 事件订阅
    # ============================================================

    async def _subscribe_swap_events(
        self,
        token_in: str, token_out: str,
        amount_in_human: float,
        token_in_decimals: int, token_out_decimals: int,
        uni_fee: int, velo_stable: bool,
    ) -> bool:
        """
        订阅两个 DEX 的 Swap 事件。
        收到事件后自动触发价格查询。
        """
        try:
            # 构造回调（闭包，捕获查价参数）
            async def on_swap_event(event_data: dict) -> None:
                self._event_count += 1
                logger.debug("收到 Swap 事件 #%d", self._event_count)
                await self._check_prices(
                    token_in, token_out, amount_in_human,
                    token_in_decimals, token_out_decimals,
                    uni_fee, velo_stable,
                )

            subscribed = False

            # 订阅 Uniswap V3 Pool 的 Swap 事件
            if self._uni_pool:
                await self.conn.subscribe_logs(
                    address=self._uni_pool,
                    topics=[self.uniswap.swap_event_topic],
                    callback=on_swap_event,
                )
                subscribed = True
                logger.info("已订阅 Uniswap V3 Swap 事件")

            # 订阅 Velodrome Pool 的 Swap 事件
            if self._velo_pool:
                await self.conn.subscribe_logs(
                    address=self._velo_pool,
                    topics=[self.velodrome.swap_event_topic],
                    callback=on_swap_event,
                )
                subscribed = True
                logger.info("已订阅 Velodrome Swap 事件")

            return subscribed

        except Exception as e:
            logger.error("WebSocket 订阅失败: %s", e)
            return False

    # ============================================================
    # 轮询降级模式
    # ============================================================

    async def _polling_loop(
        self,
        token_in: str, token_out: str,
        amount_in_human: float,
        token_in_decimals: int, token_out_decimals: int,
        uni_fee: int, velo_stable: bool,
        interval: float = 5.0,
    ) -> None:
        """
        轮询模式：如果 WebSocket 不可用，定期查价。
        不如事件驱动快，但总比没有好。
        """
        logger.info("轮询模式启动，间隔 %.1f 秒", interval)
        while self._running:
            try:
                await self._check_prices(
                    token_in, token_out, amount_in_human,
                    token_in_decimals, token_out_decimals,
                    uni_fee, velo_stable,
                )
            except Exception as e:
                logger.error("轮询查价出错: %s", e)
            await asyncio.sleep(interval)

    # ============================================================
    # 辅助方法
    # ============================================================

    def _save_snapshot(self, snapshot: PriceSnapshot) -> None:
        """保存价格快照到历史记录"""
        self.price_history.append(snapshot)
        if len(self.price_history) > self._max_history:
            self.price_history = self.price_history[-self._max_history:]

    def get_latest_spread(self) -> float:
        """获取最新价差"""
        if not self.price_history:
            return 0.0
        return self.price_history[-1].spread

    def get_stats(self) -> dict:
        """获取监听统计信息"""
        spreads = [s.spread for s in self.price_history if s.spread > 0]
        return {
            "total_checks": len(self.price_history),
            "events_received": self._event_count,
            "avg_spread": sum(spreads) / len(spreads) if spreads else 0,
            "max_spread": max(spreads) if spreads else 0,
            "opportunities": sum(
                1 for s in self.price_history
                if s.spread >= self.strategy.min_profit_threshold
            ),
        }
