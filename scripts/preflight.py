"""
部署前检查（Preflight Check）

启动机器人前运行此脚本，验证所有配置和连接是否正常：
1. 配置文件完整性
2. RPC 连接（HTTP + WebSocket）
3. 钱包余额
4. 合约部署状态
5. DEX 查价功能
6. Telegram 通知（可选）

使用方式:
    python scripts/preflight.py
    python scripts/preflight.py --mainnet   # 使用主网公开 RPC
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

# 把项目根目录加入 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def ok(msg: str) -> None:
    print(f"  \033[32m[OK]\033[0m {msg}")


def warn(msg: str) -> None:
    print(f"  \033[33m[WARN]\033[0m {msg}")


def fail(msg: str) -> None:
    print(f"  \033[31m[FAIL]\033[0m {msg}")


async def main():
    parser = argparse.ArgumentParser(description="MEV 机器人部署前检查")
    parser.add_argument("--mainnet", action="store_true", help="使用主网公开 RPC")
    args = parser.parse_args()

    print("=" * 60)
    print(" MEV 套利机器人 - 部署前检查 (Preflight)")
    print("=" * 60)
    print()

    passed = 0
    failed = 0
    warnings = 0

    # ============================================================
    # 1. 配置检查
    # ============================================================
    print("--- 1. 配置文件 ---")

    try:
        from utils.config import Config, OptimismConfig

        config = Config.from_env()
        ok(f"配置加载成功 (Chain ID: {config.optimism.chain_id})")
        passed += 1

        if args.mainnet:
            config.optimism = OptimismConfig(
                rpc_http="https://mainnet.optimism.io",
                rpc_ws="",
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
            ok("使用 Optimism 主网公开 RPC")
    except ValueError as e:
        fail(f"配置错误: {e}")
        failed += 1
        print("\n请修复 .env 文件后重试。")
        sys.exit(1)

    # 检查 .env 文件权限
    env_path = project_root / ".env"
    if env_path.exists():
        mode = oct(env_path.stat().st_mode)[-3:]
        if mode in ("600", "400"):
            ok(f".env 文件权限安全: {mode}")
            passed += 1
        else:
            warn(f".env 文件权限 {mode}，建议 chmod 600 .env")
            warnings += 1

    print()

    # ============================================================
    # 2. RPC 连接
    # ============================================================
    print("--- 2. RPC 连接 ---")

    from utils.web3_utils import ChainConnection

    conn = ChainConnection(config.optimism)

    try:
        await conn.connect()
        status = await conn.health_check()
        ok(f"HTTP 连接成功 (Block #{status['block_number']})")
        passed += 1
    except Exception as e:
        fail(f"HTTP 连接失败: {e}")
        failed += 1

    if config.optimism.rpc_ws:
        try:
            await conn.connect_ws()
            ok("WebSocket 连接成功")
            passed += 1
        except Exception as e:
            warn(f"WebSocket 连接失败: {e} (可降级为轮询模式)")
            warnings += 1
    else:
        warn("未配置 WebSocket RPC，将使用轮询模式")
        warnings += 1

    print()

    # ============================================================
    # 3. 钱包余额
    # ============================================================
    print("--- 3. 钱包余额 ---")

    try:
        from web3 import Web3

        wallet = Web3.to_checksum_address(config.wallet_address)
        balance_wei = conn.w3.eth.get_balance(wallet)
        balance_eth = balance_wei / 1e18

        ok(f"钱包: {wallet[:10]}...{wallet[-6:]}")
        if balance_eth >= 0.01:
            ok(f"ETH 余额: {balance_eth:.6f} ETH (足够支付 Gas)")
            passed += 1
        elif balance_eth > 0:
            warn(f"ETH 余额: {balance_eth:.6f} ETH (偏低，建议 > 0.01 ETH)")
            warnings += 1
        else:
            fail(f"ETH 余额: 0 (无法支付 Gas)")
            failed += 1
    except Exception as e:
        fail(f"查询余额失败: {e}")
        failed += 1

    print()

    # ============================================================
    # 4. 合约状态
    # ============================================================
    print("--- 4. 合约部署 ---")

    contract_addr = config.optimism.arbitrage_contract
    if contract_addr and len(contract_addr) == 42:
        try:
            code = conn.w3.eth.get_code(
                Web3.to_checksum_address(contract_addr)
            )
            if len(code) > 2:
                ok(f"ArbitrageExecutor 合约已部署: {contract_addr[:10]}...")
                passed += 1
            else:
                fail(f"合约地址无代码: {contract_addr}")
                failed += 1
        except Exception as e:
            fail(f"查询合约失败: {e}")
            failed += 1
    else:
        warn("未配置 ARBITRAGE_CONTRACT，将以 dry-run 模式运行")
        warnings += 1

    print()

    # ============================================================
    # 5. DEX 查价
    # ============================================================
    print("--- 5. DEX 查价 ---")

    try:
        from contracts.uniswap_v3 import UniswapV3
        from contracts.velodrome import Velodrome

        uni = UniswapV3(conn.w3, config.optimism)
        velo = Velodrome(conn.w3, config.optimism)

        # Uniswap V3 查价
        uni_price = uni.get_price(
            config.optimism.usdc, config.optimism.weth,
            1000.0, 6, 18, 3000,
        )
        if uni_price and uni_price > 0:
            ok(f"Uniswap V3: 1000 USDC → {uni_price:.6f} WETH")
            passed += 1
        else:
            fail("Uniswap V3 查价失败")
            failed += 1

        # Velodrome 查价
        velo_price = velo.get_price(
            config.optimism.usdc, config.optimism.weth,
            1000.0, 6, 18, False,
        )
        if velo_price and velo_price > 0:
            ok(f"Velodrome:  1000 USDC → {velo_price:.6f} WETH")
            passed += 1
        else:
            fail("Velodrome 查价失败")
            failed += 1

        # 价差
        if uni_price and velo_price:
            spread = abs(uni_price - velo_price) / min(uni_price, velo_price) * 100
            ok(f"当前价差: {spread:.4f}%")

    except Exception as e:
        fail(f"DEX 查价失败: {e}")
        failed += 1

    print()

    # ============================================================
    # 6. Telegram 通知
    # ============================================================
    print("--- 6. Telegram 通知 ---")

    if config.notification.telegram_enabled:
        if config.notification.telegram_bot_token and config.notification.telegram_chat_id:
            try:
                import aiohttp

                url = f"https://api.telegram.org/bot{config.notification.telegram_bot_token}/getMe"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            bot_name = data.get("result", {}).get("username", "unknown")
                            ok(f"Telegram Bot 连接正常: @{bot_name}")
                            passed += 1
                        else:
                            fail(f"Telegram Bot Token 无效 (status={resp.status})")
                            failed += 1
            except Exception as e:
                fail(f"Telegram 连接失败: {e}")
                failed += 1
        else:
            fail("Telegram 已启用但缺少 BOT_TOKEN 或 CHAT_ID")
            failed += 1
    else:
        warn("Telegram 通知未启用（运行时只写日志）")
        warnings += 1

    print()

    # ============================================================
    # 7. 总结
    # ============================================================
    await conn.close()

    print("=" * 60)
    print(f" 检查完成: {passed} 通过, {warnings} 警告, {failed} 失败")
    print("=" * 60)

    if failed > 0:
        print()
        fail("有检查项未通过，请修复后重试")
        sys.exit(1)
    elif warnings > 0:
        print()
        warn("有警告项，可以运行但建议处理")
    else:
        print()
        ok("所有检查通过！可以启动机器人。")
        print()
        print("启动命令:")
        print("  python main.py --poll          # 轮询模式（安全）")
        print("  python main.py                 # WebSocket 模式（生产）")


if __name__ == "__main__":
    asyncio.run(main())
