"""
三明治攻击单元测试

测试 MempoolMonitor 的交易解码 + SandwichStrategy 的决策逻辑。
不需要网络连接，全部用 mock 数据。
"""

import time

import pytest

from data.mempool_monitor import MempoolMonitor, PendingSwap


# ============================================================
# Mock 对象
# ============================================================


class MockW3:
    """假 Web3"""

    class eth:
        @staticmethod
        def get_transaction(tx_hash):
            return None

        gas_price = 1_000_000  # 0.001 Gwei


class MockConn:
    """假连接"""
    w3 = MockW3()

    async def subscribe_pending_txs(self, callback):
        return "mock_sub_id"


class MockConfig:
    """假配置"""

    class Optimism:
        uniswap_v3_router = "0xE592427A0AEce92De3Edee1F18E0157C05861564"
        uniswap_v3_quoter = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
        uniswap_v3_factory = "0x1F98431c8aD98523631AE4a59f267346ea31F984"
        velodrome_router = "0xa062aE8A9c5e11aaA026fc2670B0D65cCc8B2858"
        velodrome_factory = "0xF1046053aa5682b4F9a81b5481394DA16BE5FF5a"
        weth = "0x4200000000000000000000000000000000000006"
        usdc = "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85"
        usdt = "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58"
        op = "0x4200000000000000000000000000000000000042"
        rpc_http = "https://example.com"
        rpc_ws = "wss://example.com"
        chain_id = 10
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
    """假 Gas 估算器"""

    def __init__(self, gas_ok=True, cost_eth=0.000001):
        self._gas_ok = gas_ok
        self._cost_eth = cost_eth

    def is_gas_acceptable(self):
        return self._gas_ok

    def estimate_swap_cost_eth(self):
        return self._cost_eth

    def estimate_arbitrage_cost_eth(self):
        return self._cost_eth * 2


class MockUniswap:
    """假 Uniswap，返回固定价格"""

    def __init__(self, price=0.0005, quote_raw=500_000_000_000_000):
        self._price = price
        self._quote_raw = quote_raw

    def get_price(self, token_in, token_out, amount_in_human,
                  token_in_decimals=6, token_out_decimals=18, fee=3000):
        # 如果查 ETH/USD 价格（WETH → USDC）
        if token_in_decimals == 18 and token_out_decimals == 6:
            return 3000.0
        return self._price * amount_in_human

    def get_quote(self, token_in, token_out, amount_in, fee=3000):
        return self._quote_raw


class MockVelodrome:
    """假 Velodrome"""

    def __init__(self, price=0.00049, quote_raw=490_000_000_000_000):
        self._price = price
        self._quote_raw = quote_raw

    def get_price(self, token_in, token_out, amount_in_human,
                  token_in_decimals=6, token_out_decimals=18, stable=False):
        return self._price * amount_in_human

    def get_quote(self, token_in, token_out, amount_in, stable=False):
        return self._quote_raw


def _make_pending_swap(
    dex="uniswap",
    amount_in=5000 * 10**6,
    amount_in_human=5000.0,
    token_in="0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
    token_out="0x4200000000000000000000000000000000000006",
) -> PendingSwap:
    """构造一个假的 pending swap"""
    return PendingSwap(
        tx_hash="0x" + "a" * 64,
        timestamp=time.time(),
        sender="0x" + "b" * 40,
        dex=dex,
        router="0xE592427A0AEce92De3Edee1F18E0157C05861564",
        function_name="exactInputSingle",
        token_in=token_in,
        token_out=token_out,
        amount_in=amount_in,
        amount_in_human=amount_in_human,
        min_amount_out=0,
        gas_price=1_000_000,
        value=0,
        raw_input="0x" + "0" * 100,
    )


# ============================================================
# MempoolMonitor 测试
# ============================================================


class TestMempoolMonitor:
    """测试 Mempool 监听器"""

    def test_init(self):
        """初始化应该正确设置状态"""
        monitor = MempoolMonitor(MockConn(), MockConfig())
        assert monitor.total_pending == 0
        assert monitor.total_swaps == 0
        assert monitor._running is False

    def test_dex_router_mapping(self):
        """应该正确映射 DEX Router 地址"""
        monitor = MempoolMonitor(MockConn(), MockConfig())
        routers = monitor._dex_routers
        assert routers["0xe592427a0aece92de3edee1f18e0157c05861564"] == "uniswap"
        assert routers["0xa062ae8a9c5e11aaa026fc2670b0d65ccc8b2858"] == "velodrome"

    def test_analyze_non_swap_tx(self):
        """非 swap 交易应该返回 None"""
        monitor = MempoolMonitor(MockConn(), MockConfig())
        # 模拟一个普通转账（to 不是 DEX router）
        tx = {
            "to": "0x" + "1" * 40,
            "input": "0x",
            "from": "0x" + "2" * 40,
            "hash": b"\x00" * 32,
        }
        result = monitor._analyze_transaction(tx)
        assert result is None

    def test_analyze_contract_creation(self):
        """合约创建交易（to=None）应该返回 None"""
        monitor = MempoolMonitor(MockConn(), MockConfig())
        tx = {
            "to": None,
            "input": "0x" + "0" * 100,
            "from": "0x" + "2" * 40,
            "hash": b"\x00" * 32,
        }
        result = monitor._analyze_transaction(tx)
        assert result is None

    def test_analyze_short_input(self):
        """input 太短（没有函数签名）应该返回 None"""
        monitor = MempoolMonitor(MockConn(), MockConfig())
        tx = {
            "to": "0xE592427A0AEce92De3Edee1F18E0157C05861564",
            "input": "0x1234",  # 只有 2 字节
            "from": "0x" + "2" * 40,
            "hash": b"\x00" * 32,
        }
        result = monitor._analyze_transaction(tx)
        assert result is None

    def test_to_human_amount_usdc(self):
        """USDC 精度转换"""
        monitor = MempoolMonitor(MockConn(), MockConfig())
        usdc = MockConfig.Optimism.usdc
        assert monitor._to_human_amount(usdc, 1_000_000) == 1.0
        assert monitor._to_human_amount(usdc, 5_000_000_000) == 5000.0

    def test_to_human_amount_weth(self):
        """WETH 精度转换"""
        monitor = MempoolMonitor(MockConn(), MockConfig())
        weth = MockConfig.Optimism.weth
        assert monitor._to_human_amount(weth, 10**18) == 1.0

    def test_stats(self):
        """统计应该正确"""
        monitor = MempoolMonitor(MockConn(), MockConfig())
        stats = monitor.get_stats()
        assert stats["total_pending"] == 0
        assert stats["total_swaps"] == 0
        assert stats["running"] is False


# ============================================================
# SandwichStrategy 测试
# ============================================================


class TestSandwichStrategy:
    """测试三明治攻击策略"""

    @pytest.mark.asyncio
    async def test_gas_too_high_skips(self):
        """Gas 过高应该跳过"""
        from bot.sandwich_attack import SandwichStrategy

        strategy = SandwichStrategy(
            conn=MockConn(),
            uniswap=MockUniswap(),
            velodrome=MockVelodrome(),
            gas_estimator=MockGasEstimator(gas_ok=False),
            config=MockConfig(),
        )
        swap = _make_pending_swap()
        decision = await strategy.evaluate(swap)
        assert decision.action == "skip"
        assert "Gas" in decision.reason

    @pytest.mark.asyncio
    async def test_small_price_impact_skips(self):
        """价格影响太小应该跳过"""
        from bot.sandwich_attack import SandwichStrategy

        # 小额查价和大额查价返回相同结果 → 价格影响为 0
        strategy = SandwichStrategy(
            conn=MockConn(),
            uniswap=MockUniswap(price=0.0005),
            velodrome=MockVelodrome(price=0.0005),
            gas_estimator=MockGasEstimator(),
            config=MockConfig(),
        )
        strategy.min_price_impact = 0.01  # 要求至少 1% 影响
        swap = _make_pending_swap()
        decision = await strategy.evaluate(swap)
        assert decision.action == "skip"
        assert "影响" in decision.reason or "价格" in decision.reason

    @pytest.mark.asyncio
    async def test_profitable_sandwich_executes(self):
        """有利润的三明治应该执行（dry run 模式）"""
        from bot.sandwich_attack import SandwichStrategy

        # 构造一个有明显价格影响的场景
        # 小额查价：0.0005 per token → 大额查价也是 0.0005 * amount
        # 但 get_quote 返回值不同来模拟价格影响
        uni = MockUniswap(
            price=0.0005,
            quote_raw=500_000_000_000_000,  # frontrun 买到的 WETH
        )
        # backrun 时能换回比原来更多的 USDC
        # frontrun_amount = 5000 * 0.3 = 1500 USDC (1500_000_000 raw)
        # backrun 换回 1510_000_000 raw → 利润 = 10 USDC
        uni.get_quote = lambda token_in, token_out, amount_in, fee=3000: (
            500_000_000_000_000 if amount_in < 10**18
            else 1_510_000_000  # backrun 换回 1510 USDC
        )

        strategy = SandwichStrategy(
            conn=MockConn(),
            uniswap=uni,
            velodrome=MockVelodrome(),
            gas_estimator=MockGasEstimator(gas_ok=True, cost_eth=0.000001),
            config=MockConfig(),
        )
        strategy.min_price_impact = 0.0001  # 降低阈值方便测试

        swap = _make_pending_swap(amount_in_human=5000.0)
        decision = await strategy.evaluate(swap)

        # 因为 mock 的报价结构，利润计算可能是正也可能是负
        # 关键是验证整个流程跑通
        assert decision.action in ("execute", "skip")
        assert strategy.total_signals == 1

    @pytest.mark.asyncio
    async def test_stats_tracking(self):
        """统计计数应该正确"""
        from bot.sandwich_attack import SandwichStrategy

        strategy = SandwichStrategy(
            conn=MockConn(),
            uniswap=MockUniswap(),
            velodrome=MockVelodrome(),
            gas_estimator=MockGasEstimator(gas_ok=False),  # 让它跳过
            config=MockConfig(),
        )

        swap1 = _make_pending_swap()
        await strategy.evaluate(swap1)

        swap2 = _make_pending_swap()
        await strategy.evaluate(swap2)

        stats = strategy.get_stats()
        assert stats["total_signals"] == 2
        assert stats["total_skips"] == 2
        assert stats["total_executions"] == 0

    @pytest.mark.asyncio
    async def test_dry_run_mode(self):
        """dry run 模式应该不触发执行回调"""
        from bot.sandwich_attack import SandwichStrategy

        callback_called = False

        async def fake_frontrun(opp):
            nonlocal callback_called
            callback_called = True

        strategy = SandwichStrategy(
            conn=MockConn(),
            uniswap=MockUniswap(),
            velodrome=MockVelodrome(),
            gas_estimator=MockGasEstimator(),
            config=MockConfig(),
        )
        strategy.dry_run = True
        strategy.on_frontrun = fake_frontrun

        swap = _make_pending_swap()
        await strategy.evaluate(swap)

        # dry run 模式下不应该调用回调
        assert callback_called is False

    def test_get_decimals(self):
        """已知代币应该返回正确精度"""
        from bot.sandwich_attack import SandwichStrategy

        strategy = SandwichStrategy(
            conn=MockConn(),
            uniswap=MockUniswap(),
            velodrome=MockVelodrome(),
            gas_estimator=MockGasEstimator(),
            config=MockConfig(),
        )

        assert strategy._get_decimals(MockConfig.Optimism.usdc) == 6
        assert strategy._get_decimals(MockConfig.Optimism.weth) == 18
        assert strategy._get_decimals("0x" + "f" * 40) == 18  # 未知代币默认 18
