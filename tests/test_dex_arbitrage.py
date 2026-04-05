"""
DEX 套利策略单元测试

不需要网络连接，用 mock 数据测试决策逻辑。
"""

import time

import pytest

from data.price_monitor import ArbitrageOpportunity


def _make_opportunity(
    spread: float = 0.005,
    buy_price: float = 0.333,
    sell_price: float = 0.335,
    amount_in: float = 1000.0,
) -> ArbitrageOpportunity:
    """构造一个测试用套利机会"""
    return ArbitrageOpportunity(
        timestamp=time.time(),
        token_in="0x" + "a" * 40,
        token_out="0x" + "b" * 40,
        amount_in_human=amount_in,
        buy_dex="uniswap",
        sell_dex="velodrome",
        buy_price=buy_price,
        sell_price=sell_price,
        spread=spread,
        estimated_profit=sell_price - buy_price,
    )


class MockGasEstimator:
    """假的 Gas 估算器"""

    def __init__(self, gas_ok: bool = True, cost_eth: float = 0.000001):
        self._gas_ok = gas_ok
        self._cost_eth = cost_eth

    def is_gas_acceptable(self) -> bool:
        return self._gas_ok

    def estimate_arbitrage_cost_eth(self) -> float:
        return self._cost_eth


class MockUniswap:
    """假的 Uniswap，返回固定价格"""

    def get_price(self, **kwargs) -> float:
        return 3000.0  # 1 WETH = 3000 USDC


class MockVelodrome:
    pass


class MockConfig:
    """假配置"""

    class Optimism:
        weth = "0x" + "1" * 40
        usdc = "0x" + "2" * 40

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


# ============================================================
# 测试
# ============================================================


@pytest.mark.asyncio
async def test_profitable_opportunity_executes():
    """净利润为正的机会应该生成 execute 决策"""
    from bot.dex_arbitrage import DexArbitrage

    strategy = DexArbitrage(
        conn=None,
        uniswap=MockUniswap(),
        velodrome=MockVelodrome(),
        gas_estimator=MockGasEstimator(gas_ok=True, cost_eth=0.000001),
        config=MockConfig(),
    )

    opp = _make_opportunity(spread=0.01, buy_price=0.333, sell_price=0.3363)
    decision = await strategy.evaluate(opp)

    assert decision.action == "execute"
    assert decision.net_profit_usd > 0
    assert strategy.total_executions == 1


@pytest.mark.asyncio
async def test_gas_too_high_skips():
    """Gas 过高时应该跳过"""
    from bot.dex_arbitrage import DexArbitrage

    strategy = DexArbitrage(
        conn=None,
        uniswap=MockUniswap(),
        velodrome=MockVelodrome(),
        gas_estimator=MockGasEstimator(gas_ok=False),
        config=MockConfig(),
    )

    opp = _make_opportunity(spread=0.01)
    decision = await strategy.evaluate(opp)

    assert decision.action == "skip"
    assert "Gas" in decision.reason


@pytest.mark.asyncio
async def test_negative_profit_skips():
    """利润不够覆盖 Gas 和滑点时应该跳过"""
    from bot.dex_arbitrage import DexArbitrage

    strategy = DexArbitrage(
        conn=None,
        uniswap=MockUniswap(),
        velodrome=MockVelodrome(),
        gas_estimator=MockGasEstimator(gas_ok=True, cost_eth=1.0),  # 极高 Gas
        config=MockConfig(),
    )

    opp = _make_opportunity(spread=0.001, buy_price=0.333, sell_price=0.33333)
    decision = await strategy.evaluate(opp)

    assert decision.action == "skip"
    assert "负" in decision.reason or "净利润" in decision.reason


@pytest.mark.asyncio
async def test_amount_below_minimum_skips():
    """交易金额低于最小值时应该跳过"""
    from bot.dex_arbitrage import DexArbitrage

    strategy = DexArbitrage(
        conn=None,
        uniswap=MockUniswap(),
        velodrome=MockVelodrome(),
        gas_estimator=MockGasEstimator(),
        config=MockConfig(),
    )

    opp = _make_opportunity(amount_in=5.0)  # 低于 min=10
    decision = await strategy.evaluate(opp)

    assert decision.action == "skip"


@pytest.mark.asyncio
async def test_stats_tracking():
    """统计计数应该正确"""
    from bot.dex_arbitrage import DexArbitrage

    strategy = DexArbitrage(
        conn=None,
        uniswap=MockUniswap(),
        velodrome=MockVelodrome(),
        gas_estimator=MockGasEstimator(),
        config=MockConfig(),
    )

    # 一次会执行
    opp1 = _make_opportunity(spread=0.01, buy_price=0.333, sell_price=0.3363)
    await strategy.evaluate(opp1)

    # 一次会跳过
    opp2 = _make_opportunity(amount_in=5.0)
    await strategy.evaluate(opp2)

    stats = strategy.get_stats()
    assert stats["total_signals"] == 2
    assert stats["total_executions"] == 1
    assert stats["total_skips"] == 1
