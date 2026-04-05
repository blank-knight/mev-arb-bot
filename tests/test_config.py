"""
config.py 单元测试

这些测试不需要网络连接，全部用假的环境变量。
验证配置加载、类型转换、验证逻辑是否正确。
"""

import os

import pytest


# ============================================================
# 辅助：构造完整的假环境变量
# ============================================================

VALID_ENV = {
    "OPTIMISM_RPC_HTTP": "https://opt-sepolia.g.alchemy.com/v2/test-key",
    "OPTIMISM_RPC_WS": "wss://opt-sepolia.g.alchemy.com/v2/test-key",
    "OPTIMISM_CHAIN_ID": "11155420",
    "UNISWAP_V3_QUOTER": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
    "UNISWAP_V3_ROUTER": "0xE592427A0AEce92De3Edee1F18E0157C05861564",
    "UNISWAP_V3_FACTORY": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
    "VELODROME_ROUTER": "0xa062aE8A9c5e11aaA026fc2670B0D65cCc8B2858",
    "VELODROME_FACTORY": "0xF1046053aa5682b4F9a81b5481394DA16BE5FF5a",
    "WETH_ADDRESS": "0x4200000000000000000000000000000000000006",
    "USDC_ADDRESS": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
    "OP_ADDRESS": "0x4200000000000000000000000000000000000042",
    "USDT_ADDRESS": "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58",
    "PRIVATE_KEY": "0xabc123def456abc123def456abc123def456abc123def456abc123def456abc1",
    "WALLET_ADDRESS": "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD28",
    "MIN_PROFIT_THRESHOLD": "0.003",
    "MAX_SLIPPAGE": "0.003",
    "MAX_TRADE_AMOUNT": "1000",
    "MIN_TRADE_AMOUNT": "10",
    "MAX_GAS_PRICE": "0.5",
    "GAS_PRICE_STRATEGY": "dynamic",
    "LOG_LEVEL": "INFO",
    "LOG_FILE_PATH": "./logs/test.log",
    "LOG_MAX_SIZE": "10485760",
    "LOG_BACKUP_COUNT": "5",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """
    每个测试运行前：清理环境变量，防止测试之间互相污染。
    autouse=True 意味着每个测试都自动执行这个 fixture。
    """
    # 清除所有我们用到的环境变量
    for key in VALID_ENV:
        monkeypatch.delenv(key, raising=False)


def _set_env(monkeypatch, overrides=None):
    """设置完整的合法环境变量，可选覆盖部分字段"""
    env = {**VALID_ENV, **(overrides or {})}
    for key, value in env.items():
        monkeypatch.setenv(key, value)


# ============================================================
# 测试：正常加载
# ============================================================


def test_load_valid_config(monkeypatch):
    """给定完整合法的环境变量，应该成功加载所有字段"""
    _set_env(monkeypatch)

    # 注意：不传 env_path，因为环境变量已经直接设置好了
    from utils.config import Config

    config = Config.from_env("/dev/null")  # 传一个空文件，仅用 os.environ

    assert config.optimism.chain_id == 11155420
    assert config.optimism.rpc_http.startswith("https://")
    assert config.optimism.rpc_ws.startswith("wss://")
    assert config.strategy.min_profit_threshold == 0.003
    assert config.strategy.max_trade_amount == 1000.0
    assert config.wallet_address.startswith("0x")


# ============================================================
# 测试：缺少必填字段
# ============================================================


def test_missing_private_key(monkeypatch):
    """缺少 PRIVATE_KEY 时应该立刻报错"""
    _set_env(monkeypatch)
    monkeypatch.delenv("PRIVATE_KEY")

    from utils.config import Config

    with pytest.raises(ValueError, match="PRIVATE_KEY"):
        Config.from_env("/dev/null")


def test_missing_rpc_url(monkeypatch):
    """缺少 RPC URL 时应该立刻报错"""
    _set_env(monkeypatch)
    monkeypatch.delenv("OPTIMISM_RPC_HTTP")

    from utils.config import Config

    with pytest.raises(ValueError, match="OPTIMISM_RPC_HTTP"):
        Config.from_env("/dev/null")


# ============================================================
# 测试：地址格式验证
# ============================================================


def test_invalid_address_no_0x(monkeypatch):
    """地址不以 0x 开头时应该报错"""
    _set_env(monkeypatch, {"WALLET_ADDRESS": "742d35Cc6634C0532925a3b844Bc9e7595f2bD28"})

    from utils.config import Config

    with pytest.raises(ValueError, match="0x"):
        Config.from_env("/dev/null")


def test_invalid_address_wrong_length(monkeypatch):
    """地址长度不是 42 时应该报错"""
    _set_env(monkeypatch, {"WALLET_ADDRESS": "0x1234"})

    from utils.config import Config

    with pytest.raises(ValueError, match="长度"):
        Config.from_env("/dev/null")


# ============================================================
# 测试：类型转换
# ============================================================


def test_numeric_conversion(monkeypatch):
    """字符串 '0.003' 应该正确转为 float 0.003"""
    _set_env(monkeypatch)

    from utils.config import Config

    config = Config.from_env("/dev/null")
    assert isinstance(config.strategy.min_profit_threshold, float)
    assert isinstance(config.optimism.chain_id, int)
    assert isinstance(config.log.max_size, int)


# ============================================================
# 测试：安全性
# ============================================================


def test_private_key_hidden_in_repr(monkeypatch):
    """repr(config) 不应该泄露私钥"""
    _set_env(monkeypatch)

    from utils.config import Config

    config = Config.from_env("/dev/null")
    text = repr(config)
    assert "abc123" not in text  # 私钥内容不在输出里
    assert "***" in text  # 用 *** 替代


# ============================================================
# 测试：URL 格式验证
# ============================================================


def test_invalid_http_url(monkeypatch):
    """HTTP RPC URL 不以 https:// 开头时应该报错"""
    _set_env(monkeypatch, {"OPTIMISM_RPC_HTTP": "http://insecure.example.com"})

    from utils.config import Config

    with pytest.raises(ValueError, match="https://"):
        Config.from_env("/dev/null")


def test_invalid_ws_url(monkeypatch):
    """WebSocket URL 不以 wss:// 开头时应该报错"""
    _set_env(monkeypatch, {"OPTIMISM_RPC_WS": "ws://insecure.example.com"})

    from utils.config import Config

    with pytest.raises(ValueError, match="wss://"):
        Config.from_env("/dev/null")


# ============================================================
# 测试：策略参数范围
# ============================================================


def test_invalid_profit_threshold(monkeypatch):
    """利润阈值为 0 或负数时应该报错"""
    _set_env(monkeypatch, {"MIN_PROFIT_THRESHOLD": "0"})

    from utils.config import Config

    with pytest.raises(ValueError, match="MIN_PROFIT_THRESHOLD"):
        Config.from_env("/dev/null")


def test_min_greater_than_max_trade(monkeypatch):
    """最小交易金额 >= 最大交易金额时应该报错"""
    _set_env(monkeypatch, {"MIN_TRADE_AMOUNT": "2000", "MAX_TRADE_AMOUNT": "1000"})

    from utils.config import Config

    with pytest.raises(ValueError, match="MIN_TRADE_AMOUNT"):
        Config.from_env("/dev/null")
