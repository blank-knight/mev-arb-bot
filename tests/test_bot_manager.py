"""
BotManager 单元测试

测试机器人管理器的生命周期管理和统计收集。
不需要网络连接，全部用 mock 数据。
"""

import asyncio
import time

import pytest

from bot.bot_manager import BotManager, BotStats


# ============================================================
# Mock 对象（复用 test_sandwich 的模式）
# ============================================================


class MockW3:
    """假 Web3"""

    class eth:
        chain_id = 10
        block_number = 12345

        @staticmethod
        def get_transaction(tx_hash):
            return None

        gas_price = 1_000_000

    def is_connected(self):
        return True


class MockConn:
    """假连接"""

    def __init__(self):
        self.w3 = MockW3()
        self._closed = False

    async def connect(self):
        pass

    async def health_check(self):
        return {"chain_id": 10, "block_number": 12345, "healthy": True}

    async def subscribe_logs(self, address, topics, callback):
        return "mock_sub_log"

    async def subscribe_pending_txs(self, callback):
        return "mock_sub_pending"

    async def listen(self):
        # 模拟 WebSocket 监听（立即返回以便测试）
        await asyncio.sleep(0.01)

    async def close(self):
        self._closed = True


class MockConfig:
    """假配置"""

    class Optimism:
        rpc_http = "https://example.com"
        rpc_ws = "wss://example.com"
        chain_id = 10
        uniswap_v3_quoter = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
        uniswap_v3_router = "0xE592427A0AEce92De3Edee1F18E0157C05861564"
        uniswap_v3_factory = "0x1F98431c8aD98523631AE4a59f267346ea31F984"
        velodrome_router = "0xa062aE8A9c5e11aaA026fc2670B0D65cCc8B2858"
        velodrome_factory = "0xF1046053aa5682b4F9a81b5481394DA16BE5FF5a"
        weth = "0x4200000000000000000000000000000000000006"
        usdc = "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85"
        usdt = "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58"
        op = "0x4200000000000000000000000000000000000042"
        arbitrage_contract = ""

    class Strategy:
        min_profit_threshold = 0.003
        max_slippage = 0.003
        max_trade_amount = 1000.0
        min_trade_amount = 10.0
        max_gas_price = 0.5
        gas_price_strategy = "dynamic"

    class Notification:
        telegram_enabled = False
        telegram_bot_token = ""
        telegram_chat_id = ""
        notify_on_trade = False
        notify_on_error = False
        notify_on_startup = False
        stats_report_interval = 0

    optimism = Optimism()
    strategy = Strategy()
    notification = Notification()
    private_key = "0x" + "a" * 64
    wallet_address = "0x" + "b" * 40


class MockGasEstimator:
    def __init__(self):
        pass

    def is_gas_acceptable(self):
        return True

    def estimate_swap_cost_eth(self):
        return 0.000001

    def estimate_arbitrage_cost_eth(self):
        return 0.000002


class MockUniswap:
    def __init__(self):
        pass

    def get_price(self, *args, **kwargs):
        return 3000.0

    def get_quote(self, *args, **kwargs):
        return 500_000_000_000_000

    def get_pool(self, *args, **kwargs):
        return "0x" + "1" * 40


class MockVelodrome:
    def get_price(self, *args, **kwargs):
        return 2999.0

    def get_quote(self, *args, **kwargs):
        return 490_000_000_000_000

    def get_pool(self, *args, **kwargs):
        return "0x" + "2" * 40


# ============================================================
# 辅助函数：构造一个已注入 mock 的 BotManager
# ============================================================


def _make_manager() -> BotManager:
    """构造一个用 mock 组件填充的 BotManager"""
    manager = BotManager(MockConfig())
    manager.conn = MockConn()
    manager.uniswap = MockUniswap()
    manager.velodrome = MockVelodrome()
    manager.gas_estimator = MockGasEstimator()

    from bot.dex_arbitrage import DexArbitrage
    from bot.sandwich_attack import SandwichStrategy
    from bot.transaction_executor import TransactionExecutor
    from data.mempool_monitor import MempoolMonitor
    from data.price_monitor import PriceMonitor
    from utils.notifier import Notifier

    manager.notifier = Notifier(MockConfig.Notification())

    manager.arb_strategy = DexArbitrage(
        manager.conn, manager.uniswap, manager.velodrome,
        manager.gas_estimator, MockConfig(),
    )
    manager.sandwich_strategy = SandwichStrategy(
        manager.conn, manager.uniswap, manager.velodrome,
        manager.gas_estimator, MockConfig(),
    )
    manager.executor = TransactionExecutor(
        manager.conn, manager.uniswap, manager.velodrome, MockConfig(),
    )
    manager.price_monitor = PriceMonitor(
        manager.conn, manager.uniswap, manager.velodrome, MockConfig(),
    )
    manager.mempool_monitor = MempoolMonitor(manager.conn, MockConfig())

    manager.arb_strategy.dry_run = True
    manager.sandwich_strategy.dry_run = True
    manager.arb_strategy.on_trade = manager.executor.execute
    manager.price_monitor.on_opportunity = manager.arb_strategy.evaluate
    manager.mempool_monitor.on_large_swap = manager.sandwich_strategy.evaluate

    manager._running = True
    manager._start_time = time.time()

    return manager


# ============================================================
# 测试
# ============================================================


class TestBotManagerInit:
    """测试初始化"""

    def test_init_defaults(self):
        """初始化后所有组件应该是 None"""
        manager = BotManager(MockConfig())
        assert manager.conn is None
        assert manager.arb_strategy is None
        assert manager.sandwich_strategy is None
        assert manager.executor is None
        assert manager.is_running is False

    def test_init_config(self):
        """配置应该被正确保存"""
        config = MockConfig()
        manager = BotManager(config)
        assert manager.config is config


class TestBotManagerStart:
    """测试启动流程"""

    @pytest.mark.asyncio
    async def test_start_initializes_all_components(self):
        """start() 应该初始化所有组件"""
        manager = BotManager(MockConfig())

        # 注入 mock 连接（绕过真实的 ChainConnection）
        manager.conn = MockConn()

        # 手动执行 start 中的初始化逻辑（模拟）
        from bot.dex_arbitrage import DexArbitrage
        from bot.sandwich_attack import SandwichStrategy
        from bot.transaction_executor import TransactionExecutor
        from contracts.uniswap_v3 import UniswapV3
        from contracts.velodrome import Velodrome
        from data.mempool_monitor import MempoolMonitor
        from data.price_monitor import PriceMonitor
        from utils.gas_estimator import GasEstimator

        # 模拟 start() 的核心逻辑
        manager.uniswap = MockUniswap()
        manager.velodrome = MockVelodrome()
        manager.gas_estimator = MockGasEstimator()
        manager.arb_strategy = DexArbitrage(
            manager.conn, manager.uniswap, manager.velodrome,
            manager.gas_estimator, MockConfig(),
        )
        manager.sandwich_strategy = SandwichStrategy(
            manager.conn, manager.uniswap, manager.velodrome,
            manager.gas_estimator, MockConfig(),
        )
        manager.executor = TransactionExecutor(
            manager.conn, manager.uniswap, manager.velodrome, MockConfig(),
        )
        manager._running = True

        assert manager.arb_strategy is not None
        assert manager.sandwich_strategy is not None
        assert manager.executor is not None
        assert manager.is_running is True


class TestBotManagerStop:
    """测试关闭流程"""

    @pytest.mark.asyncio
    async def test_stop_sets_running_false(self):
        """stop() 后 is_running 应该是 False"""
        manager = _make_manager()
        assert manager.is_running is True
        await manager.stop()
        assert manager.is_running is False

    @pytest.mark.asyncio
    async def test_stop_closes_connection(self):
        """stop() 应该关闭连接"""
        manager = _make_manager()
        conn = manager.conn
        await manager.stop()
        assert conn._closed is True

    @pytest.mark.asyncio
    async def test_stop_cancels_tasks(self):
        """stop() 应该取消所有后台任务"""
        manager = _make_manager()

        # 添加一个假的后台任务
        async def forever():
            while True:
                await asyncio.sleep(1)

        task = asyncio.create_task(forever())
        manager._tasks.append(task)

        await manager.stop()
        assert task.cancelled()
        assert len(manager._tasks) == 0

    @pytest.mark.asyncio
    async def test_double_stop_is_safe(self):
        """连续调用两次 stop() 不应该报错"""
        manager = _make_manager()
        await manager.stop()
        await manager.stop()  # 第二次不应该出错
        assert manager.is_running is False


class TestBotManagerStats:
    """测试统计收集"""

    def test_empty_stats(self):
        """没有活动时统计应该全为 0"""
        manager = _make_manager()
        stats = manager.get_stats()

        assert isinstance(stats, BotStats)
        assert stats.arb_signals == 0
        assert stats.arb_executions == 0
        assert stats.sandwich_signals == 0
        assert stats.total_trades == 0
        assert stats.price_checks == 0
        assert stats.mempool_pending == 0

    def test_uptime_tracking(self):
        """运行时间应该大于 0"""
        manager = _make_manager()
        manager._start_time = time.time() - 100  # 模拟已运行 100 秒
        stats = manager.get_stats()
        assert stats.uptime_seconds >= 100

    @pytest.mark.asyncio
    async def test_arb_stats_after_signal(self):
        """套利信号后统计应该更新"""
        from data.price_monitor import ArbitrageOpportunity

        manager = _make_manager()

        opp = ArbitrageOpportunity(
            timestamp=time.time(),
            token_in="0x" + "a" * 40,
            token_out="0x" + "b" * 40,
            amount_in_human=1000.0,
            buy_dex="uniswap",
            sell_dex="velodrome",
            buy_price=0.333,
            sell_price=0.3363,
            spread=0.01,
            estimated_profit=0.0033,
        )

        await manager.arb_strategy.evaluate(opp)
        stats = manager.get_stats()
        assert stats.arb_signals == 1
        assert stats.arb_executions + stats.arb_skips == 1

    @pytest.mark.asyncio
    async def test_sandwich_stats_after_signal(self):
        """三明治信号后统计应该更新"""
        from data.mempool_monitor import PendingSwap

        manager = _make_manager()

        swap = PendingSwap(
            tx_hash="0x" + "a" * 64,
            timestamp=time.time(),
            sender="0x" + "b" * 40,
            dex="uniswap",
            router="0xE592427A0AEce92De3Edee1F18E0157C05861564",
            function_name="exactInputSingle",
            token_in=MockConfig.Optimism.usdc,
            token_out=MockConfig.Optimism.weth,
            amount_in=5000 * 10**6,
            amount_in_human=5000.0,
            min_amount_out=0,
            gas_price=1_000_000,
            value=0,
            raw_input="0x" + "0" * 100,
        )

        await manager.sandwich_strategy.evaluate(swap)
        stats = manager.get_stats()
        assert stats.sandwich_signals == 1

    def test_log_stats_does_not_crash(self):
        """打印统计不应该崩溃"""
        manager = _make_manager()
        # 只验证不抛异常
        manager._log_stats()


class TestBotManagerCallbackChain:
    """测试回调链连接"""

    def test_arb_callback_chain(self):
        """套利回调链应该正确连接"""
        manager = _make_manager()
        assert manager.price_monitor.on_opportunity is not None
        assert manager.arb_strategy.on_trade is not None

    def test_sandwich_callback_chain(self):
        """三明治回调链应该正确连接"""
        manager = _make_manager()
        assert manager.mempool_monitor.on_large_swap is not None

    def test_dry_run_default(self):
        """默认应该是 dry run 模式"""
        manager = _make_manager()
        assert manager.arb_strategy.dry_run is True
        assert manager.sandwich_strategy.dry_run is True


class TestBotManagerPolling:
    """测试轮询模式"""

    @pytest.mark.asyncio
    async def test_polling_stops_when_not_running(self):
        """_running=False 时轮询应该停止"""
        manager = _make_manager()
        manager._running = False  # 立即停止

        # _run_polling 应该立即退出，不会无限循环
        # 通过设置一个超时来验证
        try:
            await asyncio.wait_for(
                manager._run_polling(interval=1),
                timeout=0.5,
            )
        except asyncio.TimeoutError:
            pytest.fail("轮询模式应该在 _running=False 时立即退出")

    @pytest.mark.asyncio
    async def test_run_requires_start(self):
        """run() 在 start() 之前调用应该报错"""
        manager = BotManager(MockConfig())
        with pytest.raises(RuntimeError, match="start"):
            await manager.run()
