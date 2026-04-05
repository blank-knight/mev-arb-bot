"""
MEV 套利机器人 - 主入口

把所有模块串起来运行：
1. 加载配置
2. 通过 BotManager 初始化并启动所有模块
3. 优雅关闭

使用方式：
    # WebSocket 事件驱动模式（生产）
    python main.py

    # 轮询模式（测试网/主网只读）
    python main.py --poll

    # 使用主网公开 RPC（只读查价，不花钱）
    python main.py --mainnet --poll

    # 自定义轮询间隔
    python main.py --poll --interval 5
"""

import argparse
import asyncio
import logging
import signal
import sys

from bot.bot_manager import BotManager
from utils.config import Config, OptimismConfig
from utils.logger import setup_logger

logger = logging.getLogger(__name__)


async def run_bot(args):
    """启动机器人主流程"""

    # 1. 加载配置
    config = Config.from_env()
    setup_logger(config.log)

    # 如果指定主网，覆盖链配置（只读，不花钱）
    if args.mainnet:
        logger.info("模式: Optimism 主网（只读查价）")
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

    # 2. 创建并启动 BotManager
    manager = BotManager(config)

    # 优雅关闭
    loop = asyncio.get_event_loop()

    def handle_shutdown(sig):
        logger.info("收到退出信号 (%s)，正在关闭...", sig.name)
        asyncio.ensure_future(manager.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_shutdown, sig)

    try:
        await manager.start()

        mode = "poll" if args.poll else "ws"
        await manager.run(mode=mode, poll_interval=args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        if manager.is_running:
            await manager.stop()


def main():
    parser = argparse.ArgumentParser(description="MEV 套利机器人")
    parser.add_argument(
        "--mainnet", action="store_true",
        help="使用 Optimism 主网公开 RPC（只读查价）",
    )
    parser.add_argument(
        "--poll", action="store_true",
        help="使用轮询模式（而非 WebSocket 事件驱动）",
    )
    parser.add_argument(
        "--interval", type=int, default=10,
        help="轮询间隔秒数（默认 10）",
    )
    args = parser.parse_args()

    asyncio.run(run_bot(args))


if __name__ == "__main__":
    main()
