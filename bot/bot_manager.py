"""
机器人管理器

统一管理所有策略模块的生命周期：
1. 初始化所有组件（连接、DEX 接口、策略、执行器）
2. 并发运行套利 + 三明治两条策略线
3. 定期健康检查 + 状态报告
4. 优雅关闭（Ctrl+C / SIGTERM）

架构：
    BotManager
    ├── PriceMonitor  → DexArbitrage → TransactionExecutor  (套利线)
    ├── MempoolMonitor → SandwichStrategy                   (三明治线)
    └── 定期任务：健康检查 + 统计报告

使用方式：
    manager = BotManager(config)
    await manager.start()      # 启动所有模块
    await manager.run()         # 运行主循环（阻塞）
    await manager.stop()        # 优雅关闭
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from bot.dex_arbitrage import DexArbitrage
from bot.sandwich_attack import SandwichStrategy
from bot.transaction_executor import TransactionExecutor
from contracts.uniswap_v3 import UniswapV3
from contracts.velodrome import Velodrome
from data.mempool_monitor import MempoolMonitor
from data.price_monitor import PriceMonitor
from utils.config import Config, NotificationConfig
from utils.gas_estimator import GasEstimator
from utils.notifier import Notifier
from utils.web3_utils import ChainConnection

logger = logging.getLogger(__name__)


@dataclass
class BotStats:
    """机器人运行统计快照"""

    timestamp: float = 0.0
    uptime_seconds: float = 0.0

    # 套利统计
    arb_signals: int = 0
    arb_executions: int = 0
    arb_skips: int = 0

    # 三明治统计
    sandwich_signals: int = 0
    sandwich_executions: int = 0
    sandwich_skips: int = 0

    # 交易执行
    total_trades: int = 0
    successful_trades: int = 0

    # 监听统计
    price_checks: int = 0
    swap_events: int = 0
    mempool_pending: int = 0
    mempool_large_swaps: int = 0

    # 连接状态
    chain_healthy: bool = False
    current_block: int = 0

    # 通知统计
    notifier_sent: int = 0
    notifier_failed: int = 0


class BotManager:
    """
    机器人管理器：统一调度所有策略模块。

    职责：
    1. 组装：把各个模块连接起来（回调链）
    2. 启动：并发启动价格监听 + Mempool 监听
    3. 监控：定期健康检查 + 打印统计
    4. 关闭：按顺序停止所有模块，释放资源
    """

    def __init__(self, config: Config):
        self.config = config

        # 组件（在 start() 中初始化）
        self.conn: Optional[ChainConnection] = None
        self.uniswap: Optional[UniswapV3] = None
        self.velodrome: Optional[Velodrome] = None
        self.gas_estimator: Optional[GasEstimator] = None
        self.price_monitor: Optional[PriceMonitor] = None
        self.mempool_monitor: Optional[MempoolMonitor] = None
        self.arb_strategy: Optional[DexArbitrage] = None
        self.sandwich_strategy: Optional[SandwichStrategy] = None
        self.executor: Optional[TransactionExecutor] = None
        self.notifier: Optional[Notifier] = None

        # 运行状态
        self._running = False
        self._start_time: float = 0.0
        self._tasks: list[asyncio.Task] = []

        # 配置
        self.health_check_interval = 60  # 健康检查间隔（秒）
        self.stats_interval = 300  # 统计报告间隔（秒，5 分钟）

    async def start(self) -> None:
        """
        初始化并启动所有模块。

        顺序：
        1. 连接区块链
        2. 初始化 DEX 接口
        3. 初始化策略引擎
        4. 连接回调链
        5. 启动监听器
        """
        logger.info("=" * 60)
        logger.info("MEV 套利机器人启动")
        logger.info("=" * 60)

        self._start_time = time.time()
        chain_config = self.config.optimism

        # 1. 连接区块链
        self.conn = ChainConnection(chain_config)
        await self.conn.connect()

        status = await self.conn.health_check()
        logger.info(
            "Chain ID: %d, Block: #%d",
            status["chain_id"], status["block_number"],
        )

        # 2. 初始化 DEX 接口
        self.uniswap = UniswapV3(self.conn.w3, chain_config)
        self.velodrome = Velodrome(self.conn.w3, chain_config)
        self.gas_estimator = GasEstimator(self.conn.w3, self.config.strategy)

        # 3. 初始化策略引擎
        self.arb_strategy = DexArbitrage(
            self.conn, self.uniswap, self.velodrome,
            self.gas_estimator, self.config,
        )
        self.sandwich_strategy = SandwichStrategy(
            self.conn, self.uniswap, self.velodrome,
            self.gas_estimator, self.config,
        )
        self.executor = TransactionExecutor(
            self.conn, self.uniswap, self.velodrome, self.config,
        )

        # dry_run 模式（默认开启，安全第一）
        self.arb_strategy.dry_run = True
        self.sandwich_strategy.dry_run = True

        # 4. 初始化通知模块
        self.notifier = Notifier(self.config.notification)
        await self.notifier.start()

        # 5. 连接回调链
        #   PriceMonitor → DexArbitrage → TransactionExecutor
        self.arb_strategy.on_trade = self.executor.execute

        #   MempoolMonitor → SandwichStrategy
        self.price_monitor = PriceMonitor(
            self.conn, self.uniswap, self.velodrome, self.config,
        )
        self.price_monitor.on_opportunity = self.arb_strategy.evaluate

        self.mempool_monitor = MempoolMonitor(self.conn, self.config)
        self.mempool_monitor.on_large_swap = self.sandwich_strategy.evaluate

        self._running = True
        logger.info("所有模块初始化完成")

        # 6. 发送启动通知
        await self.notifier.notify_startup(
            mode="WebSocket / 轮询",
            dry_run=self.arb_strategy.dry_run,
        )

    async def run(self, mode: str = "ws", poll_interval: int = 10) -> None:
        """
        启动主运行循环。

        参数：
            mode: 运行模式
                - "ws": WebSocket 事件驱动（生产模式）
                - "poll": 轮询模式（测试/只读模式）
            poll_interval: 轮询间隔秒数（仅 poll 模式）
        """
        if not self._running:
            raise RuntimeError("请先调用 start()")

        logger.info("运行模式: %s", "WebSocket 事件驱动" if mode == "ws" else "轮询")

        # 启动定期任务
        self._tasks.append(
            asyncio.create_task(self._health_check_loop())
        )
        self._tasks.append(
            asyncio.create_task(self._stats_loop())
        )

        # Telegram 定期统计报告
        report_interval = self.config.notification.stats_report_interval
        if report_interval > 0 and self.notifier and self.notifier.telegram_enabled:
            self._tasks.append(
                asyncio.create_task(self._notify_stats_loop(report_interval))
            )

        if mode == "poll":
            await self._run_polling(poll_interval)
        else:
            await self._run_ws()

    async def _run_ws(self) -> None:
        """
        WebSocket 事件驱动模式。

        并发启动：
        - 价格监听（订阅 Swap 事件）
        - Mempool 监听（订阅 pending 交易）
        - WebSocket 消息循环
        """
        # 启动价格监听器（订阅 Swap 事件）
        await self.price_monitor.start()
        logger.info("价格监听器已启动")

        # 启动 Mempool 监听器（订阅 pending 交易）
        await self.mempool_monitor.start()
        logger.info("Mempool 监听器已启动")

        # 启动 WebSocket 主循环（接收并分发事件）
        logger.info("进入 WebSocket 监听主循环...")
        await self.conn.listen()

    async def _run_polling(self, interval: int) -> None:
        """
        轮询模式：定期查价，不监听 Mempool。

        适合：
        - 测试环境
        - 主网只读验证
        - WebSocket 不可用时的降级
        """
        chain_config = self.config.optimism
        cycle = 0

        logger.info("轮询模式启动，间隔 %d 秒", interval)

        while self._running:
            cycle += 1
            logger.info("--- 轮询 #%d ---", cycle)

            try:
                snapshot = await self.price_monitor.check_once(
                    token_in=chain_config.usdc,
                    token_out=chain_config.weth,
                    amount_in_human=1000.0,
                    token_in_decimals=6,
                    token_out_decimals=18,
                )

                if snapshot and snapshot.uniswap_price and snapshot.velodrome_price:
                    logger.info(
                        "Uni=%.6f WETH | Velo=%.6f WETH | 价差=%.4f%%",
                        snapshot.uniswap_price,
                        snapshot.velodrome_price,
                        snapshot.spread * 100,
                    )
            except Exception as e:
                logger.error("轮询查价失败: %s", e)

            # 每 10 轮打印统计
            if cycle % 10 == 0:
                self._log_stats()

            await asyncio.sleep(interval)

    async def stop(self) -> None:
        """
        优雅关闭所有模块。

        顺序很重要：先停监听，再关连接。
        """
        logger.info("正在关闭机器人...")
        self._running = False

        # 1. 取消定期任务
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

        # 2. 停止监听器
        if self.price_monitor:
            await self.price_monitor.stop()
        if self.mempool_monitor:
            await self.mempool_monitor.stop()

        # 3. 打印最终统计
        self._log_stats()

        # 4. 发送关闭通知
        uptime = time.time() - self._start_time if self._start_time else 0
        if self.notifier:
            stats = self.get_stats()
            await self.notifier.notify_shutdown(uptime, {
                "arb_signals": stats.arb_signals,
                "sandwich_signals": stats.sandwich_signals,
                "total_trades": stats.total_trades,
            })
            await self.notifier.stop()

        # 5. 关闭连接
        if self.conn:
            await self.conn.close()

        logger.info("机器人已关闭 (运行时长: %.0f 秒)", uptime)

    # ============================================================
    # 定期任务
    # ============================================================

    async def _health_check_loop(self) -> None:
        """定期检查区块链连接健康状态"""
        while self._running:
            await asyncio.sleep(self.health_check_interval)
            try:
                status = await self.conn.health_check()
                if not status["healthy"]:
                    logger.warning("区块链连接不健康，尝试重连...")
                    await self.conn.connect()
                else:
                    logger.debug(
                        "健康检查通过: block=#%d", status["block_number"]
                    )
            except Exception as e:
                logger.error("健康检查失败: %s", e)

    async def _stats_loop(self) -> None:
        """定期打印运行统计"""
        while self._running:
            await asyncio.sleep(self.stats_interval)
            self._log_stats()

    async def _notify_stats_loop(self, interval: int) -> None:
        """定期通过 Telegram 发送统计报告"""
        while self._running:
            await asyncio.sleep(interval)
            if self.notifier:
                stats = self.get_stats()
                await self.notifier.notify_stats({
                    "uptime": stats.uptime_seconds,
                    "arb_signals": stats.arb_signals,
                    "arb_executions": stats.arb_executions,
                    "arb_skips": stats.arb_skips,
                    "sandwich_signals": stats.sandwich_signals,
                    "sandwich_executions": stats.sandwich_executions,
                    "sandwich_skips": stats.sandwich_skips,
                    "total_trades": stats.total_trades,
                    "successful_trades": stats.successful_trades,
                    "current_block": stats.current_block,
                    "chain_healthy": stats.chain_healthy,
                })

    def _log_stats(self) -> None:
        """打印当前运行统计"""
        stats = self.get_stats()
        logger.info("=" * 50)
        logger.info("运行统计 (运行 %.0f 秒)", stats.uptime_seconds)
        logger.info("-" * 50)
        logger.info(
            "套利: 信号=%d, 执行=%d, 跳过=%d",
            stats.arb_signals, stats.arb_executions, stats.arb_skips,
        )
        logger.info(
            "三明治: 信号=%d, 执行=%d, 跳过=%d",
            stats.sandwich_signals,
            stats.sandwich_executions,
            stats.sandwich_skips,
        )
        logger.info(
            "交易: 总计=%d, 成功=%d",
            stats.total_trades, stats.successful_trades,
        )
        logger.info(
            "监听: 价格查询=%d, Swap事件=%d, Mempool=%d, 大额=%d",
            stats.price_checks, stats.swap_events,
            stats.mempool_pending, stats.mempool_large_swaps,
        )
        logger.info(
            "连接: 健康=%s, 区块=#%d",
            stats.chain_healthy, stats.current_block,
        )
        logger.info(
            "通知: 发送=%d, 失败=%d",
            stats.notifier_sent, stats.notifier_failed,
        )
        logger.info("=" * 50)

    # ============================================================
    # 状态查询
    # ============================================================

    def get_stats(self) -> BotStats:
        """收集所有模块的统计信息"""
        now = time.time()
        stats = BotStats(
            timestamp=now,
            uptime_seconds=now - self._start_time if self._start_time else 0,
        )

        if self.arb_strategy:
            arb = self.arb_strategy.get_stats()
            stats.arb_signals = arb["total_signals"]
            stats.arb_executions = arb["total_executions"]
            stats.arb_skips = arb["total_skips"]

        if self.sandwich_strategy:
            sw = self.sandwich_strategy.get_stats()
            stats.sandwich_signals = sw["total_signals"]
            stats.sandwich_executions = sw["total_executions"]
            stats.sandwich_skips = sw["total_skips"]

        if self.executor:
            ex = self.executor.get_stats()
            stats.total_trades = ex["total_trades"]
            stats.successful_trades = ex["successful_trades"]

        if self.price_monitor:
            pm = self.price_monitor.get_stats()
            stats.price_checks = pm["total_checks"]
            stats.swap_events = pm["events_received"]

        if self.mempool_monitor:
            mm = self.mempool_monitor.get_stats()
            stats.mempool_pending = mm["total_pending"]
            stats.mempool_large_swaps = mm["total_large_swaps"]

        if self.conn and self.conn.w3:
            stats.chain_healthy = self.conn.w3.is_connected()
            try:
                stats.current_block = self.conn.w3.eth.block_number
            except Exception:
                stats.current_block = 0

        if self.notifier:
            ns = self.notifier.get_stats()
            stats.notifier_sent = ns["total_sent"]
            stats.notifier_failed = ns["total_failed"]

        return stats

    @property
    def is_running(self) -> bool:
        return self._running
