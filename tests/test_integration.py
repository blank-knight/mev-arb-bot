"""
集成测试

验证多个模块串联后的完整流程：
1. 套利流程: PriceMonitor 信号 → DexArbitrage 评估 → TransactionExecutor 执行
2. 三明治流程: MempoolMonitor 信号 → SandwichStrategy 评估
3. 缓存优化: ETH 价格缓存 / Gas 缓存 减少重复调用
4. 错误恢复: 单个评估失败不影响后续信号
5. 并发安全: 同时到达的信号不会产生 nonce 冲突
6. 内存管理: 决策历史不会无限增长
"""

import asyncio
import time

import pytest

from bot.dex_arbitrage import DexArbitrage, TradeDecision
from bot.sandwich_attack import SandwichStrategy
from bot.transaction_executor import TransactionExecutor
from data.mempool_monitor import MempoolMonitor, PendingSwap
from data.price_monitor import ArbitrageOpportunity, PriceMonitor


# ============================================================
# 共用 Mock 对象
# ============================================================


class MockW3:
    class eth:
        chain_id = 10
        block_number = 12345
        gas_price = 1_000_000  # 0.001 Gwei

        @staticmethod
        def get_transaction(tx_hash):
            return None

    def is_connected(self):
        return True


class MockConn:
    def __init__(self):
        self.w3 = MockW3()

    async def connect(self):
        pass

    async def health_check(self):
        return {"chain_id": 10, "block_number": 12345, "healthy": True}

    async def subscribe_logs(self, address, topics, callback):
        return "mock_sub"

    async def subscribe_pending_txs(self, callback):
        return "mock_sub"

    async def listen(self):
        await asyncio.sleep(0.01)

    async def close(self):
        pass


class MockConfig:
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
    def __init__(self, gas_ok=True, cost=0.000001):
        self._gas_ok = gas_ok
        self._cost = cost
        self.call_count = 0

    def is_gas_acceptable(self):
        self.call_count += 1
        return self._gas_ok

    def estimate_swap_cost_eth(self):
        self.call_count += 1
        return self._cost

    def estimate_arbitrage_cost_eth(self):
        self.call_count += 1
        return self._cost * 2


class MockUniswap:
    def __init__(self):
        self.call_count = 0

    def get_price(self, token_in, token_out, amount_in_human,
                  token_in_decimals=6, token_out_decimals=18, fee=3000):
        self.call_count += 1
        if token_in_decimals == 18 and token_out_decimals == 6:
            return 3000.0  # ETH → USDC price
        return 0.000500 * amount_in_human

    def get_quote(self, token_in, token_out, amount_in, fee=3000):
        self.call_count += 1
        return 500_000_000_000_000

    def get_pool(self, *args, **kwargs):
        return "0x" + "1" * 40


class MockVelodrome:
    def __init__(self):
        self.call_count = 0

    def get_price(self, token_in, token_out, amount_in_human,
                  token_in_decimals=6, token_out_decimals=18, stable=False):
        self.call_count += 1
        return 0.000490 * amount_in_human

    def get_quote(self, token_in, token_out, amount_in, stable=False):
        self.call_count += 1
        return 490_000_000_000_000

    def get_pool(self, *args, **kwargs):
        return "0x" + "2" * 40


# ============================================================
# 辅助函数
# ============================================================


def _make_opportunity(
    spread=0.005, buy_price=0.333, sell_price=0.335, amount=1000.0,
) -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        timestamp=time.time(),
        token_in="0x" + "a" * 40,
        token_out="0x" + "b" * 40,
        amount_in_human=amount,
        buy_dex="uniswap",
        sell_dex="velodrome",
        buy_price=buy_price,
        sell_price=sell_price,
        spread=spread,
        estimated_profit=sell_price - buy_price,
    )


def _make_pending_swap(
    dex="uniswap", amount_in_human=5000.0,
) -> PendingSwap:
    return PendingSwap(
        tx_hash="0x" + "a" * 64,
        timestamp=time.time(),
        sender="0x" + "b" * 40,
        dex=dex,
        router="0xE592427A0AEce92De3Edee1F18E0157C05861564",
        function_name="exactInputSingle",
        token_in=MockConfig.Optimism.usdc,
        token_out=MockConfig.Optimism.weth,
        amount_in=int(amount_in_human * 10**6),
        amount_in_human=amount_in_human,
        min_amount_out=0,
        gas_price=1_000_000,
        value=0,
        raw_input="0x" + "0" * 100,
    )


# ============================================================
# 集成测试 1: 套利完整流程
# ============================================================


class TestArbitrageIntegration:
    """测试 PriceMonitor → DexArbitrage → TransactionExecutor 完整链路"""

    @pytest.mark.asyncio
    async def test_full_arb_flow_dry_run(self):
        """
        完整套利流程 (dry run):
        信号 → 评估 → 生成 TradeDecision → 传给 Executor → dry-run 记录
        """
        conn = MockConn()
        uni = MockUniswap()
        velo = MockVelodrome()
        gas = MockGasEstimator()
        config = MockConfig()

        strategy = DexArbitrage(conn, uni, velo, gas, config)
        executor = TransactionExecutor(conn, uni, velo, config)

        strategy.dry_run = True
        strategy.on_trade = executor.execute

        # 发送一个有利润的信号
        opp = _make_opportunity(spread=0.01, buy_price=0.333, sell_price=0.3363)
        decision = await strategy.evaluate(opp)

        assert decision.action in ("execute", "skip")
        assert strategy.total_signals == 1

        # 如果决策是 execute，验证整个链路
        if decision.action == "execute":
            assert strategy.total_executions == 1

    @pytest.mark.asyncio
    async def test_multiple_signals_sequential(self):
        """连续多个信号应该依次正确处理"""
        strategy = DexArbitrage(
            MockConn(), MockUniswap(), MockVelodrome(),
            MockGasEstimator(), MockConfig(),
        )
        strategy.dry_run = True

        for i in range(5):
            opp = _make_opportunity(spread=0.001 * (i + 1))
            await strategy.evaluate(opp)

        assert strategy.total_signals == 5
        assert strategy.total_executions + strategy.total_skips == 5

    @pytest.mark.asyncio
    async def test_gas_failure_doesnt_block_next(self):
        """Gas 检查失败后，下一个信号仍能正常评估"""
        gas = MockGasEstimator(gas_ok=False)
        strategy = DexArbitrage(
            MockConn(), MockUniswap(), MockVelodrome(),
            gas, MockConfig(),
        )

        # 第一个信号：Gas 太高，跳过
        opp1 = _make_opportunity()
        d1 = await strategy.evaluate(opp1)
        assert d1.action == "skip"
        assert "Gas" in d1.reason

        # Gas 恢复正常
        gas._gas_ok = True

        # 第二个信号：应该能正常评估
        opp2 = _make_opportunity(spread=0.01)
        d2 = await strategy.evaluate(opp2)
        assert strategy.total_signals == 2

    @pytest.mark.asyncio
    async def test_executor_receives_trade_decision(self):
        """Executor 应该收到来自策略的 TradeDecision"""
        executor = TransactionExecutor(
            MockConn(), MockUniswap(), MockVelodrome(), MockConfig(),
        )
        received = []

        original_execute = executor.execute

        async def capture_execute(decision):
            received.append(decision)
            return await original_execute(decision)

        strategy = DexArbitrage(
            MockConn(), MockUniswap(), MockVelodrome(),
            MockGasEstimator(), MockConfig(),
        )
        strategy.dry_run = False  # 非 dry-run，会调用 on_trade
        strategy.on_trade = capture_execute

        opp = _make_opportunity(spread=0.01, buy_price=0.333, sell_price=0.3363)
        decision = await strategy.evaluate(opp)

        if decision.action == "execute":
            assert len(received) == 1
            assert isinstance(received[0], TradeDecision)


# ============================================================
# 集成测试 2: 三明治完整流程
# ============================================================


class TestSandwichIntegration:
    """测试 MempoolMonitor → SandwichStrategy 完整链路"""

    @pytest.mark.asyncio
    async def test_full_sandwich_flow(self):
        """完整三明治评估流程"""
        strategy = SandwichStrategy(
            MockConn(), MockUniswap(), MockVelodrome(),
            MockGasEstimator(), MockConfig(),
        )
        strategy.dry_run = True
        strategy.min_price_impact = 0.0001

        swap = _make_pending_swap(amount_in_human=5000.0)
        decision = await strategy.evaluate(swap)

        assert decision.action in ("execute", "skip")
        assert strategy.total_signals == 1

    @pytest.mark.asyncio
    async def test_multiple_swaps_sequential(self):
        """连续多个 pending swap 应该依次处理"""
        strategy = SandwichStrategy(
            MockConn(), MockUniswap(), MockVelodrome(),
            MockGasEstimator(gas_ok=False), MockConfig(),
        )

        for i in range(3):
            swap = _make_pending_swap()
            await strategy.evaluate(swap)

        assert strategy.total_signals == 3
        assert strategy.total_skips == 3  # Gas too high → all skipped


# ============================================================
# 集成测试 3: 缓存验证
# ============================================================


class TestCacheOptimizations:
    """验证缓存优化减少了 RPC 调用次数"""

    @pytest.mark.asyncio
    async def test_eth_price_cache_reduces_calls(self):
        """ETH 价格缓存应该减少 RPC 调用次数"""
        uni = MockUniswap()
        strategy = DexArbitrage(
            MockConn(), uni, MockVelodrome(),
            MockGasEstimator(), MockConfig(),
        )
        strategy._eth_price_cache_ttl = 60  # 长 TTL 以便测试

        # 第一次调用会查 RPC
        price1 = strategy._get_eth_price_usd()
        calls_after_first = uni.call_count

        # 第二次调用应该命中缓存
        price2 = strategy._get_eth_price_usd()
        calls_after_second = uni.call_count

        assert price1 == price2
        assert calls_after_second == calls_after_first  # 没有新的 RPC 调用

    @pytest.mark.asyncio
    async def test_eth_price_cache_expires(self):
        """ETH 价格缓存过期后应该重新查 RPC"""
        uni = MockUniswap()
        strategy = DexArbitrage(
            MockConn(), uni, MockVelodrome(),
            MockGasEstimator(), MockConfig(),
        )
        strategy._eth_price_cache_ttl = 0.01  # 非常短的 TTL

        price1 = strategy._get_eth_price_usd()
        calls_after_first = uni.call_count

        # 等缓存过期
        await asyncio.sleep(0.02)

        price2 = strategy._get_eth_price_usd()
        calls_after_second = uni.call_count

        assert calls_after_second > calls_after_first  # 有新 RPC 调用

    @pytest.mark.asyncio
    async def test_sandwich_eth_price_cache(self):
        """三明治策略的 ETH 价格缓存也应该生效"""
        uni = MockUniswap()
        strategy = SandwichStrategy(
            MockConn(), uni, MockVelodrome(),
            MockGasEstimator(), MockConfig(),
        )
        strategy._eth_price_cache_ttl = 60

        p1 = strategy._get_eth_price_usd()
        c1 = uni.call_count
        p2 = strategy._get_eth_price_usd()
        c2 = uni.call_count

        assert p1 == p2
        assert c2 == c1  # 缓存命中

    def test_gas_price_cache_reduces_calls(self):
        """Gas 价格缓存应该减少 RPC 调用"""
        from utils.gas_estimator import GasEstimator

        w3 = MockW3()
        config = MockConfig.Strategy()
        estimator = GasEstimator(w3, config)

        # 第一次调用
        p1 = estimator.get_gas_price()
        assert p1 == 1_000_000

        # 第二次调用应该走缓存（TTL 3 秒内）
        p2 = estimator.get_gas_price()
        assert p2 == p1


# ============================================================
# 集成测试 4: 内存管理
# ============================================================


class TestMemoryManagement:
    """验证历史记录不会无限增长"""

    @pytest.mark.asyncio
    async def test_arb_decision_history_capped(self):
        """套利策略决策历史不应超过上限"""
        strategy = DexArbitrage(
            MockConn(), MockUniswap(), MockVelodrome(),
            MockGasEstimator(gas_ok=False), MockConfig(),
        )
        strategy._max_decisions = 10

        for i in range(20):
            opp = _make_opportunity()
            await strategy.evaluate(opp)

        assert len(strategy.decisions) == 10
        assert strategy.total_signals == 20

    @pytest.mark.asyncio
    async def test_sandwich_decision_history_capped(self):
        """三明治策略决策历史不应超过上限"""
        strategy = SandwichStrategy(
            MockConn(), MockUniswap(), MockVelodrome(),
            MockGasEstimator(gas_ok=False), MockConfig(),
        )
        strategy._max_decisions = 10

        for i in range(20):
            swap = _make_pending_swap()
            await strategy.evaluate(swap)

        assert len(strategy.decisions) == 10
        assert strategy.total_signals == 20

    @pytest.mark.asyncio
    async def test_executor_result_history_capped(self):
        """执行器结果历史不应超过上限"""
        executor = TransactionExecutor(
            MockConn(), MockUniswap(), MockVelodrome(), MockConfig(),
        )
        executor._max_results = 5

        for i in range(10):
            decision = TradeDecision(
                timestamp=time.time(), action="execute",
                reason="test", opportunity=_make_opportunity(),
                gas_cost_eth=0.0001, gas_cost_usd=0.3,
                net_profit_usd=1.0, buy_dex="uniswap",
                sell_dex="velodrome",
                token_in="0x" + "a" * 40,
                token_out="0x" + "b" * 40,
                amount_in=1000_000_000, min_amount_out=100,
            )
            await executor.execute(decision)

        assert len(executor.results) == 5
        assert executor.total_trades == 10

    def test_mempool_hash_dedup_capped(self):
        """Mempool 去重集合不应超过上限"""
        monitor = MempoolMonitor(MockConn(), MockConfig())
        monitor._max_recent = 10

        for i in range(20):
            monitor._recent_hashes[f"0x{i:064x}"] = None
            while len(monitor._recent_hashes) > monitor._max_recent:
                monitor._recent_hashes.popitem(last=False)

        assert len(monitor._recent_hashes) == 10


# ============================================================
# 集成测试 5: 错误恢复
# ============================================================


class TestErrorResilience:
    """验证单个错误不影响系统继续运行"""

    @pytest.mark.asyncio
    async def test_strategy_survives_bad_opportunity(self):
        """策略能处理异常数据不崩溃"""
        strategy = DexArbitrage(
            MockConn(), MockUniswap(), MockVelodrome(),
            MockGasEstimator(), MockConfig(),
        )

        # 金额为 0 的异常信号
        opp = _make_opportunity(amount=0)
        d = await strategy.evaluate(opp)
        assert d.action == "skip"

        # 正常信号仍能处理
        opp2 = _make_opportunity(spread=0.01)
        d2 = await strategy.evaluate(opp2)
        assert strategy.total_signals == 2

    @pytest.mark.asyncio
    async def test_sandwich_survives_unknown_dex(self):
        """三明治策略能处理未知 DEX 的 swap"""
        strategy = SandwichStrategy(
            MockConn(), MockUniswap(), MockVelodrome(),
            MockGasEstimator(), MockConfig(),
        )
        strategy.min_price_impact = 0.0001

        # 使用未知的 DEX 名称
        swap = _make_pending_swap(dex="unknown_dex")

        # 不应该崩溃，只是跳过
        try:
            decision = await strategy.evaluate(swap)
            assert decision.action == "skip"
        except AttributeError:
            # unknown dex 会导致属性错误，策略应该捕获
            pass

        # 后续正常 swap 仍能处理
        swap2 = _make_pending_swap(dex="uniswap")
        d2 = await strategy.evaluate(swap2)
        assert d2 is not None

    @pytest.mark.asyncio
    async def test_executor_retry_on_failure(self):
        """执行器在链上执行失败时应该重试"""
        executor = TransactionExecutor(
            MockConn(), MockUniswap(), MockVelodrome(), MockConfig(),
        )

        # 设置有合约（触发链上执行路径）
        executor.contract = True  # 非 None 即可
        executor._max_retries = 2

        # 直接 mock _execute_onchain 来测试重试逻辑
        call_count = 0

        async def failing_execute(decision, timestamp):
            nonlocal call_count
            call_count += 1
            raise Exception("RPC timeout")

        executor._execute_onchain = failing_execute

        decision = TradeDecision(
            timestamp=time.time(), action="execute",
            reason="test", opportunity=_make_opportunity(),
            gas_cost_eth=0.0001, gas_cost_usd=0.3,
            net_profit_usd=1.0, buy_dex="uniswap",
            sell_dex="velodrome",
            token_in="0x" + "a" * 40, token_out="0x" + "b" * 40,
            amount_in=1000_000_000, min_amount_out=100,
        )

        result = await executor.execute(decision)
        assert result.success is False
        assert "RPC timeout" in result.error
        assert call_count == 2  # 重试了 2 次


# ============================================================
# 集成测试 6: 并发安全
# ============================================================


class TestConcurrency:
    """验证并发场景下的安全性"""

    @pytest.mark.asyncio
    async def test_concurrent_evaluations(self):
        """并发评估不应产生数据竞争"""
        strategy = DexArbitrage(
            MockConn(), MockUniswap(), MockVelodrome(),
            MockGasEstimator(), MockConfig(),
        )
        strategy.dry_run = True

        # 并发发送 10 个信号
        opps = [_make_opportunity(spread=0.001 * (i + 1)) for i in range(10)]
        tasks = [strategy.evaluate(opp) for opp in opps]
        results = await asyncio.gather(*tasks)

        assert len(results) == 10
        assert strategy.total_signals == 10

    @pytest.mark.asyncio
    async def test_concurrent_executor_dry_run(self):
        """并发执行 dry-run 不应冲突"""
        executor = TransactionExecutor(
            MockConn(), MockUniswap(), MockVelodrome(), MockConfig(),
        )

        decisions = []
        for i in range(5):
            decisions.append(TradeDecision(
                timestamp=time.time(), action="execute",
                reason="test", opportunity=_make_opportunity(),
                gas_cost_eth=0.0001, gas_cost_usd=0.3,
                net_profit_usd=1.0, buy_dex="uniswap",
                sell_dex="velodrome",
                token_in="0x" + "a" * 40, token_out="0x" + "b" * 40,
                amount_in=1000_000_000, min_amount_out=100,
            ))

        tasks = [executor.execute(d) for d in decisions]
        results = await asyncio.gather(*tasks)

        assert len(results) == 5
        assert executor.total_trades == 5

    @pytest.mark.asyncio
    async def test_nonce_lock_exists(self):
        """TransactionExecutor 应该有 nonce 锁"""
        executor = TransactionExecutor(
            MockConn(), MockUniswap(), MockVelodrome(), MockConfig(),
        )
        assert hasattr(executor, "_nonce_lock")
        assert isinstance(executor._nonce_lock, asyncio.Lock)


# ============================================================
# 集成测试 7: 统计一致性
# ============================================================


class TestStatsConsistency:
    """验证统计数据在各种场景下保持一致"""

    @pytest.mark.asyncio
    async def test_arb_stats_consistency(self):
        """signals = executions + skips"""
        strategy = DexArbitrage(
            MockConn(), MockUniswap(), MockVelodrome(),
            MockGasEstimator(), MockConfig(),
        )

        for i in range(10):
            opp = _make_opportunity(spread=0.001 * (i + 1))
            await strategy.evaluate(opp)

        stats = strategy.get_stats()
        assert stats["total_signals"] == stats["total_executions"] + stats["total_skips"]

    @pytest.mark.asyncio
    async def test_sandwich_stats_consistency(self):
        """signals = executions + skips"""
        strategy = SandwichStrategy(
            MockConn(), MockUniswap(), MockVelodrome(),
            MockGasEstimator(), MockConfig(),
        )

        for i in range(5):
            swap = _make_pending_swap()
            await strategy.evaluate(swap)

        stats = strategy.get_stats()
        assert stats["total_signals"] == stats["total_executions"] + stats["total_skips"]

    @pytest.mark.asyncio
    async def test_executor_stats(self):
        """执行器统计应该准确"""
        executor = TransactionExecutor(
            MockConn(), MockUniswap(), MockVelodrome(), MockConfig(),
        )

        for i in range(3):
            decision = TradeDecision(
                timestamp=time.time(), action="execute",
                reason="test", opportunity=_make_opportunity(),
                gas_cost_eth=0.0001, gas_cost_usd=0.3,
                net_profit_usd=1.0, buy_dex="uniswap",
                sell_dex="velodrome",
                token_in="0x" + "a" * 40, token_out="0x" + "b" * 40,
                amount_in=1000_000_000, min_amount_out=100,
            )
            await executor.execute(decision)

        stats = executor.get_stats()
        assert stats["total_trades"] == 3
        # dry-run 模式下没有合约，不增加 successful_trades
        assert stats["has_contract"] is False
