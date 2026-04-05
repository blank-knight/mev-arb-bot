"""
Mempool 监听器（WebSocket 事件驱动）

核心逻辑：
1. 通过 WebSocket 订阅 newPendingTransactions
2. 获取 pending 交易的完整数据
3. 解码 swap 函数调用（识别 Uniswap / Velodrome 的 swap 方法）
4. 过滤大额 swap → 触发三明治攻击回调

这是三明治攻击的"眼睛"。

什么是 Mempool？
    当用户发起一笔交易时，交易不会立刻上链，而是先进入"内存池"（Mempool）。
    交易在 Mempool 里等待被 sequencer（Optimism 的出块者）打包。
    在这个等待窗口内，我们可以看到交易的内容（比如谁在哪个 DEX 买了多少币），
    然后决定是否抢跑。

Optimism 的特殊性：
    Optimism 使用单一 sequencer，交易按到达顺序（FIFO）排列，
    不像 L1 那样按 Gas Price 排序。所以三明治攻击的时间窗口更短、
    竞争模型不同。我们能做的是尽快发送前置交易，利用网络延迟优势。

使用方式：
    monitor = MempoolMonitor(conn, config)
    monitor.on_large_swap = my_callback
    await monitor.start()
"""

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Coroutine, Optional

from web3 import Web3

from utils.config import Config
from utils.web3_utils import ChainConnection

logger = logging.getLogger(__name__)

# ============================================================
# Swap 函数签名（用于识别交易类型）
# ============================================================

# Uniswap V3 SwapRouter 函数签名（前 4 字节）
# exactInputSingle((address,address,uint24,address,uint256,uint256,uint256,uint160))
UNISWAP_EXACT_INPUT_SINGLE = "0x414bf389"
# exactInput((bytes,address,uint256,uint256,uint256))
UNISWAP_EXACT_INPUT = "0xc04b8d59"

# Velodrome V2 Router 函数签名
# swapExactTokensForTokens(uint256,uint256,(address,address,bool,address)[],address,uint256)
VELODROME_SWAP_EXACT = "0x8a657e67"

# 已知的 DEX Router 地址 → DEX 名称
DEX_ROUTERS: dict[str, str] = {}  # 在 __init__ 中根据 config 填充


@dataclass
class PendingSwap:
    """
    一笔等待上链的 swap 交易的解析结果。

    这是从 mempool 中捕获到的"猎物"信息：
    - 谁在哪个 DEX 做了多大的 swap
    - 用了多少 Gas（帮助判断交易优先级）
    - 交易方向（买入还是卖出）
    """

    tx_hash: str           # 交易哈希
    timestamp: float       # 发现时间
    sender: str            # 发送者地址
    dex: str               # 目标 DEX（"uniswap" 或 "velodrome"）
    router: str            # Router 合约地址
    function_name: str     # 调用的函数名
    token_in: str          # 输入代币
    token_out: str         # 输出代币
    amount_in: int         # 输入金额（最小单位）
    amount_in_human: float # 输入金额（人类可读）
    min_amount_out: int    # 最少输出（滑点保护）
    gas_price: int         # Gas Price（Wei）
    value: int             # 交易附带的 ETH（Wei）
    raw_input: str         # 原始 calldata（十六进制）


class MempoolMonitor:
    """
    Mempool 监听器：发现大额 swap 交易。

    工作流程：
    ┌─────────────────────────┐
    │ WebSocket 订阅 pending  │
    └──────────┬──────────────┘
               ↓
    ┌─────────────────────────┐
    │ 收到 tx_hash            │
    └──────────┬──────────────┘
               ↓
    ┌─────────────────────────┐
    │ 获取交易详情             │ → 不是 swap → 跳过
    └──────────┬──────────────┘
               ↓
    ┌─────────────────────────┐
    │ 解码 swap 调用           │ → 解码失败 → 跳过
    └──────────┬──────────────┘
               ↓
    ┌─────────────────────────┐
    │ 金额 > 阈值？            │ → 否 → 跳过
    └──────────┬──────────────┘
               ↓ 是
    ┌─────────────────────────┐
    │ 触发 on_large_swap 回调  │ → 交给三明治策略
    └─────────────────────────┘
    """

    def __init__(self, conn: ChainConnection, config: Config):
        self.conn = conn
        self.config = config
        self.w3 = conn.w3

        # 三明治策略回调
        self.on_large_swap: Optional[
            Callable[[PendingSwap], Coroutine]
        ] = None

        # 构建 DEX Router 地址 → 名称映射
        self._dex_routers: dict[str, str] = {
            config.optimism.uniswap_v3_router.lower(): "uniswap",
            config.optimism.velodrome_router.lower(): "velodrome",
        }

        # 最小金额阈值（低于此金额的 swap 不值得三明治）
        # 默认 $500 USDC（小额交易价格影响太小，利润不够覆盖 Gas）
        self.min_amount_threshold = 500 * 10**6  # 500 USDC (6 decimals)

        # 已知代币精度缓存
        self._token_decimals: dict[str, int] = {
            config.optimism.usdc.lower(): 6,
            config.optimism.usdt.lower(): 6,
            config.optimism.weth.lower(): 18,
            config.optimism.op.lower(): 18,
        }

        # 统计
        self._running = False
        self.total_pending = 0      # 收到的 pending tx 总数
        self.total_swaps = 0        # 识别出的 swap 数
        self.total_large_swaps = 0  # 超过阈值的大额 swap 数
        # 去重（OrderedDict 保持插入顺序，O(1) 查找和删除）
        self._recent_hashes: OrderedDict[str, None] = OrderedDict()
        self._max_recent = 1000

    async def start(self) -> None:
        """
        启动 Mempool 监听。

        通过 WebSocket 订阅 newPendingTransactions，
        收到新的 pending tx hash 后自动获取并分析。
        """
        logger.info(
            "启动 Mempool 监听 (最小金额阈值: %d)",
            self.min_amount_threshold,
        )

        self._running = True

        try:
            await self.conn.subscribe_pending_txs(self._on_pending_tx)
            logger.info("Mempool 订阅成功，等待 pending 交易...")
        except Exception as e:
            logger.error("Mempool 订阅失败: %s", e)
            # 降级为轮询模式
            logger.warning("降级为轮询 pending 模式 (1 秒间隔)")
            asyncio.create_task(self._polling_loop())

    async def stop(self) -> None:
        """停止 Mempool 监听"""
        self._running = False
        logger.info(
            "Mempool 监听已停止 (pending=%d, swaps=%d, large=%d)",
            self.total_pending, self.total_swaps, self.total_large_swaps,
        )

    async def _on_pending_tx(self, tx_hash: str) -> None:
        """
        收到一个 pending 交易哈希时的处理。

        步骤：
        1. 去重检查
        2. 获取交易详情（eth_getTransactionByHash）
        3. 检查是否是 DEX swap
        4. 解码参数
        5. 金额超过阈值 → 通知策略层
        """
        self.total_pending += 1

        # 去重
        if isinstance(tx_hash, dict):
            # 有些节点返回完整交易对象
            tx_hash = tx_hash.get("hash", tx_hash)
        if isinstance(tx_hash, bytes):
            tx_hash = tx_hash.hex()
        tx_hash = str(tx_hash)

        if tx_hash in self._recent_hashes:
            return
        self._recent_hashes[tx_hash] = None

        # 防止无限增长：超过上限时删除最早的条目
        while len(self._recent_hashes) > self._max_recent:
            self._recent_hashes.popitem(last=False)

        try:
            # 获取交易详情
            tx = self.w3.eth.get_transaction(tx_hash)
            if tx is None:
                return

            # 分析交易
            swap = self._analyze_transaction(tx)
            if swap is not None:
                self.total_swaps += 1

                # 检查金额阈值
                if swap.amount_in >= self.min_amount_threshold:
                    self.total_large_swaps += 1
                    logger.info(
                        "发现大额 swap: %s, DEX=%s, 金额=%.2f, tx=%s",
                        swap.function_name, swap.dex,
                        swap.amount_in_human, swap.tx_hash[:18],
                    )
                    if self.on_large_swap:
                        await self.on_large_swap(swap)

        except Exception as e:
            # pending tx 可能已经被打包或丢弃，忽略错误
            logger.debug("处理 pending tx 出错: %s", e)

    def _analyze_transaction(self, tx) -> Optional[PendingSwap]:
        """
        分析一笔交易，判断是否是 DEX swap。

        怎么判断？
        1. 看 to 地址是否是已知的 DEX Router
        2. 看 input data 的前 4 字节（函数签名）是否是 swap 函数

        为什么看前 4 字节？
        Solidity 调用合约函数时，calldata 的前 4 字节是函数签名的 keccak256 哈希。
        例如 exactInputSingle 的签名是 0x414bf389。
        通过这 4 字节就能知道用户调用了哪个函数。
        """
        to_addr = tx.get("to")
        if to_addr is None:
            return None  # 合约创建交易

        to_lower = to_addr.lower()
        input_data = tx.get("input", "")
        if isinstance(input_data, bytes):
            input_data = "0x" + input_data.hex()

        # 至少要有函数签名（4 字节 = 0x + 8 字符）
        if len(input_data) < 10:
            return None

        # 检查是否是已知 DEX Router
        dex_name = self._dex_routers.get(to_lower)
        if dex_name is None:
            return None

        # 提取函数签名
        func_sig = input_data[:10].lower()

        # 根据 DEX 和函数签名解码
        if dex_name == "uniswap":
            return self._decode_uniswap_swap(tx, func_sig, input_data)
        elif dex_name == "velodrome":
            return self._decode_velodrome_swap(tx, func_sig, input_data)

        return None

    def _decode_uniswap_swap(
        self, tx, func_sig: str, input_data: str
    ) -> Optional[PendingSwap]:
        """
        解码 Uniswap V3 swap 交易。

        exactInputSingle 参数布局（ABI 编码）：
        - offset 0:  tokenIn (address, 32 bytes, 左填充)
        - offset 32: tokenOut (address)
        - offset 64: fee (uint24)
        - offset 96: recipient (address)
        - offset 128: deadline (uint256)
        - offset 160: amountIn (uint256)
        - offset 192: amountOutMinimum (uint256)
        - offset 224: sqrtPriceLimitX96 (uint160)

        注意：Solidity ABI 编码中，struct 参数会先有一个 offset 指针，
        实际数据从 offset 指向的位置开始。
        """
        if func_sig == UNISWAP_EXACT_INPUT_SINGLE:
            try:
                # 去掉 0x 和函数签名（4 bytes = 8 hex chars）
                data = input_data[10:]

                # exactInputSingle 的参数是一个 struct（tuple），
                # ABI 编码时先写 offset，然后是实际数据
                # offset 在 data[0:64]，通常是 0x20 (32)
                # 实际 struct 数据从 offset*2 开始
                offset = int(data[0:64], 16) * 2  # 转为 hex 字符偏移

                # struct 内部布局
                token_in = "0x" + data[offset + 24:offset + 64]
                token_out = "0x" + data[offset + 64 + 24:offset + 128]
                # fee at offset+128 to offset+192
                amount_in = int(data[offset + 320:offset + 384], 16)
                min_amount_out = int(data[offset + 384:offset + 448], 16)

                amount_in_human = self._to_human_amount(
                    token_in, amount_in
                )

                return PendingSwap(
                    tx_hash=tx["hash"].hex() if isinstance(tx["hash"], bytes) else str(tx["hash"]),
                    timestamp=time.time(),
                    sender=tx["from"],
                    dex="uniswap",
                    router=tx["to"],
                    function_name="exactInputSingle",
                    token_in=Web3.to_checksum_address(token_in),
                    token_out=Web3.to_checksum_address(token_out),
                    amount_in=amount_in,
                    amount_in_human=amount_in_human,
                    min_amount_out=min_amount_out,
                    gas_price=tx.get("gasPrice", 0),
                    value=tx.get("value", 0),
                    raw_input=input_data,
                )
            except (ValueError, IndexError) as e:
                logger.debug("解码 Uniswap exactInputSingle 失败: %s", e)
                return None

        elif func_sig == UNISWAP_EXACT_INPUT:
            # exactInput 是多跳路由，解码更复杂，暂不支持
            logger.debug("跳过 Uniswap exactInput（多跳路由，暂不支持）")
            return None

        return None

    def _decode_velodrome_swap(
        self, tx, func_sig: str, input_data: str
    ) -> Optional[PendingSwap]:
        """
        解码 Velodrome V2 swap 交易。

        swapExactTokensForTokens 参数：
        - amountIn (uint256)
        - amountOutMin (uint256)
        - routes (Route[])  — 动态数组，解码较复杂
        - to (address)
        - deadline (uint256)
        """
        if func_sig != VELODROME_SWAP_EXACT:
            return None

        try:
            data = input_data[10:]

            # 前两个参数是简单类型
            amount_in = int(data[0:64], 16)
            min_amount_out = int(data[64:128], 16)

            # routes 是动态数组，offset 在 data[128:192]
            routes_offset = int(data[128:192], 16) * 2
            routes_length = int(data[routes_offset:routes_offset + 64], 16)

            if routes_length == 0:
                return None

            # 第一个 Route 的数据（每个 Route 有 4 个字段，各 32 bytes = 256 hex）
            # Route struct: (from, to, stable, factory)
            # 但作为动态数组元素，每个 Route 的 offset 先列出来
            # 然后实际数据在各自的 offset 处
            route_data_start = routes_offset + 64  # 跳过 length
            first_route_offset = int(
                data[route_data_start:route_data_start + 64], 16
            ) * 2
            actual_start = routes_offset + 64 + first_route_offset

            token_in = "0x" + data[actual_start + 24:actual_start + 64]
            token_out = "0x" + data[actual_start + 64 + 24:actual_start + 128]

            amount_in_human = self._to_human_amount(token_in, amount_in)

            return PendingSwap(
                tx_hash=tx["hash"].hex() if isinstance(tx["hash"], bytes) else str(tx["hash"]),
                timestamp=time.time(),
                sender=tx["from"],
                dex="velodrome",
                router=tx["to"],
                function_name="swapExactTokensForTokens",
                token_in=Web3.to_checksum_address(token_in),
                token_out=Web3.to_checksum_address(token_out),
                amount_in=amount_in,
                amount_in_human=amount_in_human,
                min_amount_out=min_amount_out,
                gas_price=tx.get("gasPrice", 0),
                value=tx.get("value", 0),
                raw_input=input_data,
            )
        except (ValueError, IndexError) as e:
            logger.debug("解码 Velodrome swap 失败: %s", e)
            return None

    def _to_human_amount(self, token_addr: str, raw_amount: int) -> float:
        """
        把最小单位金额转为人类可读数字。

        例如：1000000 (USDC, 6 decimals) → 1.0
              1000000000000000000 (WETH, 18 decimals) → 1.0
        """
        decimals = self._token_decimals.get(token_addr.lower(), 18)
        return raw_amount / (10 ** decimals)

    async def _polling_loop(self, interval: float = 1.0) -> None:
        """
        轮询模式：定期检查 pending 交易。

        当 WebSocket 不可用时降级使用。
        通过 eth_getBlock("pending") 获取 pending 区块中的交易。
        """
        logger.info("Mempool 轮询模式启动，间隔 %.1f 秒", interval)
        while self._running:
            try:
                block = self.w3.eth.get_block("pending", full_transactions=True)
                for tx in block.get("transactions", []):
                    if isinstance(tx, dict):
                        tx_hash = tx.get("hash", "")
                        if isinstance(tx_hash, bytes):
                            tx_hash = tx_hash.hex()
                        swap = self._analyze_transaction(tx)
                        if swap and swap.amount_in >= self.min_amount_threshold:
                            self.total_large_swaps += 1
                            if self.on_large_swap:
                                await self.on_large_swap(swap)
            except Exception as e:
                logger.debug("轮询 pending 出错: %s", e)
            await asyncio.sleep(interval)

    def get_stats(self) -> dict:
        """获取监听统计"""
        return {
            "total_pending": self.total_pending,
            "total_swaps": self.total_swaps,
            "total_large_swaps": self.total_large_swaps,
            "swap_rate": (
                self.total_swaps / self.total_pending
                if self.total_pending > 0
                else 0
            ),
            "running": self._running,
        }
