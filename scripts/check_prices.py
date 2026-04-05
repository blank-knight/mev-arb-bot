"""
价格查询验证脚本

查询 Uniswap V3 和 Velodrome 的实时价格，计算价差。
用于验证合约接口是否正常工作。

注意：需要连接 Optimism 主网才能查到真实价格（测试网可能没有流动性）。

用法：
    python scripts/check_prices.py
    python scripts/check_prices.py --mainnet   # 连接主网（只读查价，不花钱）
"""

import asyncio
import os
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from utils.config import Config, OptimismConfig
from utils.logger import setup_logger
from utils.web3_utils import ChainConnection
from contracts.uniswap_v3 import UniswapV3
from contracts.velodrome import Velodrome


async def main():
    print("=" * 60)
    print("MEV 套利机器人 - 价格查询验证")
    print("=" * 60)

    # 判断是否使用主网
    use_mainnet = "--mainnet" in sys.argv

    if use_mainnet:
        # 使用免费公开 RPC 查主网价格（只读，不花钱）
        print("\n使用 Optimism 主网（只读查价）")
        config = Config.from_env()
        setup_logger(config.log)

        # 覆盖为主网公开 RPC
        mainnet_config = OptimismConfig(
            rpc_http="https://mainnet.optimism.io",
            rpc_ws="",  # 主网 WS 不用
            chain_id=10,
            uniswap_v3_quoter=config.optimism.uniswap_v3_quoter,
            uniswap_v3_router=config.optimism.uniswap_v3_router,
            uniswap_v3_factory=config.optimism.uniswap_v3_factory,
            velodrome_router=config.optimism.velodrome_router,
            velodrome_factory=config.optimism.velodrome_factory,
            weth="0x4200000000000000000000000000000000000006",
            usdc="0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
            op="0x4200000000000000000000000000000000000042",
            usdt="0x94b008aA00579c1307B0EF2c499aD98a8ce58e58",
        )
        conn = ChainConnection(mainnet_config)
    else:
        print("\n使用 Optimism Sepolia 测试网")
        config = Config.from_env()
        setup_logger(config.log)
        mainnet_config = config.optimism
        conn = ChainConnection(config.optimism)

    # 连接
    try:
        await conn.connect()
    except ConnectionError as e:
        print(f"\n[FAIL] 连接失败: {e}")
        sys.exit(1)

    status = await conn.health_check()
    print(f"  Chain ID: {status['chain_id']}")
    print(f"  Block:    #{status['block_number']}")

    # 创建 DEX 接口
    uni = UniswapV3(conn.w3, mainnet_config)
    velo = Velodrome(conn.w3, mainnet_config)

    USDC = mainnet_config.usdc
    WETH = mainnet_config.weth
    amount = 1000.0  # 1000 USDC

    # ---- 查 Pool 地址 ----
    print(f"\n--- 查找 Pool 地址 ---")
    uni_pool = uni.get_pool(USDC, WETH, fee=3000)
    velo_pool = velo.get_pool(USDC, WETH, stable=False)
    print(f"  Uniswap V3 Pool (0.3%): {uni_pool or '未找到'}")
    print(f"  Velodrome Pool (volatile): {velo_pool or '未找到'}")

    # ---- 查价格 ----
    print(f"\n--- 价格查询 ({amount} USDC → WETH) ---")

    uni_price = uni.get_price(
        USDC, WETH, amount, token_in_decimals=6, token_out_decimals=18
    )
    velo_price = velo.get_price(
        USDC, WETH, amount, token_in_decimals=6, token_out_decimals=18
    )

    print(f"  Uniswap V3:  {uni_price or 'N/A'} WETH")
    print(f"  Velodrome:   {velo_price or 'N/A'} WETH")

    if uni_price and velo_price:
        if uni_price > velo_price:
            spread = (uni_price - velo_price) / velo_price * 100
            print(f"\n  价差: {spread:.4f}% (Uni > Velo)")
            print(f"  方向: 在 Velodrome 买, 在 Uniswap 卖")
        else:
            spread = (velo_price - uni_price) / uni_price * 100
            print(f"\n  价差: {spread:.4f}% (Velo > Uni)")
            print(f"  方向: 在 Uniswap 买, 在 Velodrome 卖")

        if spread >= 0.3:
            print(f"  ⚡ 套利机会！价差 > 0.3%")
        else:
            print(f"  ✋ 价差不足，不执行套利")
    else:
        print("\n  ⚠️ 至少一个 DEX 查价失败，可能池子不存在")

    # ---- ETH 价格 ----
    print(f"\n--- ETH 价格 ---")
    eth_price = uni.get_price(
        WETH, USDC, 1.0, token_in_decimals=18, token_out_decimals=6
    )
    print(f"  1 WETH = {eth_price or 'N/A'} USDC")

    await conn.close()
    print(f"\n{'=' * 60}")
    print("价格查询验证完成")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(main())
