"""
Web3 连接管理模块

管理与 Optimism 链的两种连接：
- HTTP Provider：用于调用合约（查价、发交易），请求/响应模式
- WebSocket：用于订阅链上事件（Swap 事件、Pending TX），长连接推送模式

使用方式：
    from utils.web3_utils import ChainConnection
    from utils.config import Config

    config = Config.from_env()
    conn = ChainConnection(config.optimism)
    await conn.connect()

    # 查区块号（HTTP）
    status = await conn.health_check()
    print(status["block_number"])

    # 订阅事件（WebSocket，Phase 2 实现）
    await conn.subscribe_logs(pool_address, swap_topic, on_swap)
"""

import asyncio
import json
import logging
from typing import Any, Callable, Coroutine, Optional

import websockets
from web3 import Web3
from web3.providers import HTTPProvider

from utils.config import OptimismConfig

logger = logging.getLogger(__name__)

# WebSocket 重连参数
WS_RECONNECT_BASE_DELAY = 1  # 初始重连等待秒数
WS_RECONNECT_MAX_DELAY = 32  # 最大重连等待秒数
WS_RECONNECT_MAX_RETRIES = 5  # 最大连续失败次数


class ChainConnection:
    """
    管理一条链的 HTTP + WebSocket 双连接。

    为什么要两种连接？
    - HTTP 更稳定，适合"问一个答一个"的合约调用
    - WebSocket 适合"有事通知我"的事件推送

    它们各司其职，混用容易出问题。
    """

    def __init__(self, config: OptimismConfig):
        self.config = config
        self.w3: Optional[Web3] = None
        self._ws = None  # WebSocket 连接实例
        self._ws_running = False  # WebSocket 监听循环是否在运行
        self._subscriptions: dict[str, Callable] = {}  # 订阅ID → 回调函数

    # ============================================================
    # HTTP 连接
    # ============================================================

    async def connect(self) -> None:
        """
        建立 HTTP 连接并验证可用性。

        做了什么：
        1. 用 RPC URL 创建 Web3 实例
        2. 检查连接是否成功（is_connected）
        3. 验证 chain_id 是否匹配配置

        如果连接失败，立刻抛异常——不要让程序带着坏连接继续跑。
        """
        logger.info("正在连接 Optimism RPC: %s", self.config.rpc_http)

        self.w3 = Web3(HTTPProvider(self.config.rpc_http))

        if not self.w3.is_connected():
            raise ConnectionError(
                f"无法连接 RPC 节点: {self.config.rpc_http}"
            )

        # 验证 chain_id：防止配置错误连到了错误的链
        actual_chain_id = self.w3.eth.chain_id
        if actual_chain_id != self.config.chain_id:
            raise ConnectionError(
                f"Chain ID 不匹配！配置: {self.config.chain_id}, "
                f"实际: {actual_chain_id}"
            )

        block = self.w3.eth.block_number
        logger.info(
            "已连接 Optimism (chain_id=%d, block=#%d)",
            actual_chain_id,
            block,
        )

    async def health_check(self) -> dict[str, Any]:
        """
        检查连接健康状态。

        返回 dict 包含：
        - chain_id: 链 ID
        - block_number: 最新区块号
        - healthy: 是否健康

        什么时候用？
        - 启动时验证连接
        - 运行中定期检查（防止连接静默断开）
        """
        if self.w3 is None or not self.w3.is_connected():
            return {"chain_id": 0, "block_number": 0, "healthy": False}

        try:
            chain_id = self.w3.eth.chain_id
            block = self.w3.eth.block_number
            return {
                "chain_id": chain_id,
                "block_number": block,
                "healthy": True,
            }
        except Exception as e:
            logger.error("健康检查失败: %s", e)
            return {"chain_id": 0, "block_number": 0, "healthy": False}

    # ============================================================
    # WebSocket 连接
    # ============================================================

    async def connect_ws(self) -> None:
        """
        建立 WebSocket 长连接。

        WebSocket 用于订阅链上事件（eth_subscribe）：
        - "logs": 合约事件日志（例如 Swap 事件）
        - "newPendingTransactions": Mempool 未确认交易

        这个方法只建立连接，具体订阅由 subscribe_logs() 等方法完成。
        """
        logger.info("正在建立 WebSocket 连接: %s", self.config.rpc_ws)

        try:
            self._ws = await websockets.connect(
                self.config.rpc_ws,
                ping_interval=20,  # 每 20 秒发心跳，防止连接被服务器断开
                ping_timeout=10,
                close_timeout=5,
            )
            logger.info("WebSocket 连接已建立")
        except Exception as e:
            logger.error("WebSocket 连接失败: %s", e)
            raise ConnectionError(f"WebSocket 连接失败: {e}") from e

    async def subscribe_logs(
        self,
        address: str,
        topics: list[str],
        callback: Callable[[dict], Coroutine],
    ) -> str:
        """
        通过 WebSocket 订阅合约事件日志。

        参数：
            address: 合约地址（例如 Uniswap V3 Pool 地址）
            topics: 事件主题列表（例如 [Swap 事件的 keccak256 签名]）
            callback: 收到事件时的异步回调函数

        返回：
            订阅 ID（用于取消订阅）

        这是 Phase 2（价格监听）的核心：
        当有人在 Uniswap 交易时，Pool 合约会发出 Swap 事件，
        我们订阅这个事件，一有交易就立刻触发价差检查。
        """
        if self._ws is None:
            await self.connect_ws()

        # 构造 eth_subscribe 请求
        subscribe_msg = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_subscribe",
            "params": [
                "logs",
                {"address": address, "topics": topics},
            ],
        })

        await self._ws.send(subscribe_msg)
        response = await self._ws.recv()
        result = json.loads(response)

        if "error" in result:
            raise ValueError(f"订阅失败: {result['error']}")

        sub_id = result["result"]
        self._subscriptions[sub_id] = callback
        logger.info("已订阅合约事件 (sub_id=%s, address=%s)", sub_id, address)
        return sub_id

    async def subscribe_pending_txs(
        self,
        callback: Callable[[str], Coroutine],
    ) -> str:
        """
        通过 WebSocket 订阅 Mempool pending 交易。

        参数：
            callback: 收到 pending TX hash 时的异步回调函数

        这是 Phase 3（三明治攻击）的核心：
        有人发出 swap 交易但还没上链时，我们立刻看到，
        然后决定是否抢跑。
        """
        if self._ws is None:
            await self.connect_ws()

        subscribe_msg = json.dumps({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "eth_subscribe",
            "params": ["newPendingTransactions"],
        })

        await self._ws.send(subscribe_msg)
        response = await self._ws.recv()
        result = json.loads(response)

        if "error" in result:
            raise ValueError(f"订阅失败: {result['error']}")

        sub_id = result["result"]
        self._subscriptions[sub_id] = callback
        logger.info("已订阅 Mempool pending 交易 (sub_id=%s)", sub_id)
        return sub_id

    async def listen(self) -> None:
        """
        WebSocket 监听主循环。

        启动后持续接收消息，根据订阅 ID 分发到对应的回调函数。
        如果连接断开，自动触发重连。

        使用方式（通常在 bot_manager 中启动）：
            asyncio.create_task(conn.listen())
        """
        self._ws_running = True
        retry_count = 0

        while self._ws_running:
            try:
                if self._ws is None:
                    await self.connect_ws()
                    retry_count = 0  # 连接成功，重置计数

                # 持续接收 WebSocket 消息
                async for raw_msg in self._ws:
                    msg = json.loads(raw_msg)

                    # eth_subscribe 推送的消息格式
                    if msg.get("method") == "eth_subscription":
                        sub_id = msg["params"]["subscription"]
                        data = msg["params"]["result"]

                        callback = self._subscriptions.get(sub_id)
                        if callback:
                            try:
                                await callback(data)
                            except Exception as e:
                                logger.error(
                                    "回调处理出错 (sub=%s): %s", sub_id, e
                                )

            except websockets.ConnectionClosed as e:
                logger.warning("WebSocket 连接断开: %s", e)
                self._ws = None
            except Exception as e:
                logger.error("WebSocket 异常: %s", e)
                self._ws = None

            # 重连逻辑：指数退避
            if self._ws_running:
                retry_count += 1
                if retry_count > WS_RECONNECT_MAX_RETRIES:
                    logger.critical(
                        "WebSocket 连续重连 %d 次失败，停止监听",
                        WS_RECONNECT_MAX_RETRIES,
                    )
                    self._ws_running = False
                    break

                delay = min(
                    WS_RECONNECT_BASE_DELAY * (2 ** (retry_count - 1)),
                    WS_RECONNECT_MAX_DELAY,
                )
                logger.info("将在 %d 秒后重连 (第 %d 次)", delay, retry_count)
                await asyncio.sleep(delay)

    async def close(self) -> None:
        """关闭所有连接，释放资源。"""
        self._ws_running = False
        if self._ws:
            await self._ws.close()
            self._ws = None
            logger.info("WebSocket 连接已关闭")
        self._subscriptions.clear()
        logger.info("所有连接已关闭")
