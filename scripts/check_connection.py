"""
快速验证脚本：测试能否连上 Optimism Sepolia

用法：
    python scripts/check_connection.py

前提：
    1. 已安装依赖 (pip install -r requirements.txt)
    2. 已复制 .env.example 为 .env 并填入 Alchemy API Key
"""

import asyncio
import sys
from pathlib import Path

# 把项目根目录加入 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from utils.config import Config
from utils.logger import setup_logger
from utils.web3_utils import ChainConnection


async def main():
    # 1. 加载配置
    print("=" * 50)
    print("MEV 套利机器人 - 连接检查")
    print("=" * 50)

    try:
        config = Config.from_env()
        print(f"\n[OK] 配置加载成功")
        print(f"  链 ID: {config.optimism.chain_id}")
        print(f"  RPC:   {config.optimism.rpc_http[:50]}...")
    except ValueError as e:
        print(f"\n[FAIL] 配置错误: {e}")
        print("  请检查 .env 文件是否正确填写")
        sys.exit(1)

    # 2. 初始化日志
    setup_logger(config.log)

    # 3. 测试 HTTP 连接
    print(f"\n--- HTTP 连接测试 ---")
    conn = ChainConnection(config.optimism)
    try:
        await conn.connect()
        status = await conn.health_check()
        print(f"  [OK] 已连接")
        print(f"  Chain ID:    {status['chain_id']}")
        print(f"  Block:       #{status['block_number']}")
    except ConnectionError as e:
        print(f"  [FAIL] 连接失败: {e}")
        sys.exit(1)

    # 4. 测试 WebSocket 连接
    print(f"\n--- WebSocket 连接测试 ---")
    try:
        await conn.connect_ws()
        print(f"  [OK] WebSocket 已连接")
    except ConnectionError as e:
        print(f"  [WARN] WebSocket 连接失败: {e}")
        print(f"  （HTTP 仍然可用，WebSocket 非必须但推荐）")

    await conn.close()

    # 5. 总结
    print(f"\n{'=' * 50}")
    print(f"连接检查完成！基础设施就绪。")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    asyncio.run(main())
