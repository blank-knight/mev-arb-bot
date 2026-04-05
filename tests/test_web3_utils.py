"""
web3_utils.py 测试

分两类：
- 不需要网络的测试（mock）
- 需要真实 RPC 的集成测试（标记 @pytest.mark.integration）

运行方式：
    pytest tests/test_web3_utils.py -v              # 只跑不需要网络的
    pytest tests/test_web3_utils.py -v -m integration  # 只跑集成测试
    pytest tests/test_web3_utils.py -v --run-integration  # 全部跑
"""

import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio

from utils.config import OptimismConfig

# ============================================================
# 测试用的假配置
# ============================================================

FAKE_CONFIG = OptimismConfig(
    rpc_http="https://fake-rpc.example.com",
    rpc_ws="wss://fake-rpc.example.com",
    chain_id=11155420,
    uniswap_v3_quoter="0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
    uniswap_v3_router="0xE592427A0AEce92De3Edee1F18E0157C05861564",
    uniswap_v3_factory="0x1F98431c8aD98523631AE4a59f267346ea31F984",
    velodrome_router="0xa062aE8A9c5e11aaA026fc2670B0D65cCc8B2858",
    velodrome_factory="0xF1046053aa5682b4F9a81b5481394DA16BE5FF5a",
    weth="0x4200000000000000000000000000000000000006",
    usdc="0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
    op="0x4200000000000000000000000000000000000042",
    usdt="0x94b008aA00579c1307B0EF2c499aD98a8ce58e58",
)


# ============================================================
# 不需要网络的测试
# ============================================================


@pytest.mark.asyncio
async def test_connect_fails_with_bad_rpc():
    """连接到不存在的 RPC 时应该抛 ConnectionError"""
    from utils.web3_utils import ChainConnection

    conn = ChainConnection(FAKE_CONFIG)
    with pytest.raises(ConnectionError):
        await conn.connect()


@pytest.mark.asyncio
async def test_health_check_returns_unhealthy_when_not_connected():
    """未连接时 health_check 应该返回 healthy=False"""
    from utils.web3_utils import ChainConnection

    conn = ChainConnection(FAKE_CONFIG)
    # 没有调用 connect()
    status = await conn.health_check()
    assert status["healthy"] is False
    assert status["block_number"] == 0


@pytest.mark.asyncio
async def test_connect_chain_id_mismatch():
    """实际 chain_id 和配置不匹配时应该抛 ConnectionError"""
    from utils.web3_utils import ChainConnection

    conn = ChainConnection(FAKE_CONFIG)

    # mock Web3 返回不同的 chain_id
    mock_w3 = MagicMock()
    mock_w3.is_connected.return_value = True
    mock_w3.eth.chain_id = 999  # 错误的 chain_id

    with patch("utils.web3_utils.Web3", return_value=mock_w3):
        with pytest.raises(ConnectionError, match="Chain ID"):
            await conn.connect()


@pytest.mark.asyncio
async def test_close_clears_state():
    """close() 应该清理所有状态"""
    from utils.web3_utils import ChainConnection

    conn = ChainConnection(FAKE_CONFIG)
    conn._subscriptions = {"sub1": lambda x: x}
    await conn.close()
    assert len(conn._subscriptions) == 0
    assert conn._ws is None
    assert conn._ws_running is False


# ============================================================
# 集成测试（需要真实 RPC，标记 integration）
# ============================================================


def _get_real_config() -> OptimismConfig:
    """从环境变量构造真实的 Optimism Sepolia 配置"""
    rpc_http = os.getenv("OPTIMISM_RPC_HTTP", "")
    rpc_ws = os.getenv("OPTIMISM_RPC_WS", "")

    if not rpc_http or "YOUR_API_KEY" in rpc_http:
        pytest.skip("需要在 .env 中配置真实的 OPTIMISM_RPC_HTTP")

    return OptimismConfig(
        rpc_http=rpc_http,
        rpc_ws=rpc_ws,
        chain_id=11155420,
        uniswap_v3_quoter="0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        uniswap_v3_router="0xE592427A0AEce92De3Edee1F18E0157C05861564",
        uniswap_v3_factory="0x1F98431c8aD98523631AE4a59f267346ea31F984",
        velodrome_router="0xa062aE8A9c5e11aaA026fc2670B0D65cCc8B2858",
        velodrome_factory="0xF1046053aa5682b4F9a81b5481394DA16BE5FF5a",
        weth="0x4200000000000000000000000000000000000006",
        usdc="0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
        op="0x4200000000000000000000000000000000000042",
        usdt="0x94b008aA00579c1307B0EF2c499aD98a8ce58e58",
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_real_http_connection():
    """连接真实的 Optimism Sepolia，获取区块号"""
    from dotenv import load_dotenv

    load_dotenv()

    from utils.web3_utils import ChainConnection

    config = _get_real_config()
    conn = ChainConnection(config)
    await conn.connect()
    status = await conn.health_check()

    assert status["healthy"] is True
    assert status["chain_id"] == 11155420
    assert status["block_number"] > 0

    print(f"\n  Optimism Sepolia block: #{status['block_number']}")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_real_websocket_connection():
    """连接真实的 WebSocket，验证能建立连接"""
    from dotenv import load_dotenv

    load_dotenv()

    from utils.web3_utils import ChainConnection

    config = _get_real_config()

    if not config.rpc_ws or "YOUR_API_KEY" in config.rpc_ws:
        pytest.skip("需要在 .env 中配置真实的 OPTIMISM_RPC_WS")

    conn = ChainConnection(config)
    await conn.connect_ws()
    assert conn._ws is not None
    await conn.close()
