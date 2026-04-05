"""
Velodrome V2 合约接口（Optimism）

Velodrome 是 Optimism 上最大的原生 DEX，基于 Solidly 模型。
和 Uniswap V3 的区别：
- Uniswap V3: 集中流动性，多个手续费等级
- Velodrome V2: 两种池子类型（volatile 和 stable），用 Router 查价

我们用 Router 的 getAmountsOut() 来查价格。

使用方式：
    velo = Velodrome(conn.w3, config.optimism)
    price = velo.get_price(token_in=USDC, token_out=WETH, amount_in_human=1000.0)
"""

import json
import logging
from pathlib import Path
from typing import Optional

from web3 import Web3
from web3.contract import Contract

from utils.config import OptimismConfig

logger = logging.getLogger(__name__)

ABI_DIR = Path(__file__).parent / "abis"


class Velodrome:
    """
    Velodrome V2 合约交互封装。

    什么是 Velodrome？
    Optimism 链上交易量最大的 DEX。它有两种池子：
    - volatile（波动池）：普通交易对，如 WETH/USDC
    - stable（稳定池）：稳定币对，如 USDC/USDT，用特殊曲线减少滑点

    我们主要用 volatile 池，因为套利目标是 WETH/USDC 这类波动对。
    """

    def __init__(self, w3: Web3, config: OptimismConfig):
        self.w3 = w3
        self.config = config

        # 加载合约
        self.router = self._load_contract(
            config.velodrome_router, "velodrome_router.json"
        )
        self.factory = self._load_contract(
            config.velodrome_factory, "velodrome_factory.json"
        )
        self._pool_abi = self._load_abi("velodrome_pool.json")

        # Velodrome Swap 事件主题（和 Uniswap V3 的签名不同）
        self.swap_event_topic = Web3.keccak(
            text="Swap(address,address,uint256,uint256,uint256,uint256)"
        ).hex()

    def get_pool(
        self, token_a: str, token_b: str, stable: bool = False
    ) -> Optional[str]:
        """
        查找 Velodrome 交易池地址。

        参数：
            token_a: 代币 A 地址
            token_b: 代币 B 地址
            stable: 是否稳定池（False=volatile, True=stable）

        返回：
            Pool 地址，不存在返回 None
        """
        pool_address = self.factory.functions.getPool(
            Web3.to_checksum_address(token_a),
            Web3.to_checksum_address(token_b),
            stable,
        ).call()

        if pool_address == "0x0000000000000000000000000000000000000000":
            pool_type = "stable" if stable else "volatile"
            logger.warning(
                "Velodrome 池子不存在: %s/%s (%s)",
                token_a[:10], token_b[:10], pool_type,
            )
            return None

        logger.debug("Velodrome Pool: %s (stable=%s)", pool_address, stable)
        return pool_address

    def get_pool_contract(self, pool_address: str) -> Contract:
        """根据 Pool 地址创建合约实例"""
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(pool_address),
            abi=self._pool_abi,
        )

    def get_quote(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        stable: bool = False,
    ) -> Optional[int]:
        """
        查询报价：用 amount_in 个 token_in 能换多少 token_out。

        参数：
            token_in: 卖出代币地址
            token_out: 买入代币地址
            amount_in: 卖出数量（最小单位）
            stable: 是否走稳定池

        返回：
            能换到的数量（最小单位），失败返回 None

        Velodrome 的查价方式：
        用 Router.getAmountsOut()，传入一条"路由"（Route），
        路由描述了"从哪个代币到哪个代币、走哪种池子"。
        """
        try:
            # 构造路由：一条直达路由
            route = (
                Web3.to_checksum_address(token_in),   # from
                Web3.to_checksum_address(token_out),   # to
                stable,                                 # volatile or stable
                Web3.to_checksum_address(self.config.velodrome_factory),  # factory
            )

            amounts = self.router.functions.getAmountsOut(
                amount_in, [route]
            ).call()

            # amounts[0] = 输入量, amounts[1] = 输出量
            amount_out = amounts[-1]

            logger.debug(
                "Velodrome 报价: %s → %s, in=%d, out=%d, stable=%s",
                token_in[:10], token_out[:10], amount_in, amount_out, stable,
            )
            return amount_out

        except Exception as e:
            logger.error("Velodrome 报价失败: %s", e)
            return None

    def get_price(
        self,
        token_in: str,
        token_out: str,
        amount_in_human: float,
        token_in_decimals: int = 6,
        token_out_decimals: int = 18,
        stable: bool = False,
    ) -> Optional[float]:
        """
        查询人类可读的价格。

        参数和返回值同 UniswapV3.get_price()。

        示例：
            # 1000 USDC 能换多少 WETH？
            weth = velo.get_price(USDC, WETH, 1000.0, token_in_decimals=6)
            # → 0.332
        """
        amount_in_raw = int(amount_in_human * (10**token_in_decimals))
        amount_out_raw = self.get_quote(token_in, token_out, amount_in_raw, stable)

        if amount_out_raw is None:
            return None

        return amount_out_raw / (10**token_out_decimals)

    # ============================================================
    # 内部方法
    # ============================================================

    def _load_abi(self, filename: str) -> list:
        abi_path = ABI_DIR / filename
        with open(abi_path) as f:
            return json.load(f)

    def _load_contract(self, address: str, abi_filename: str) -> Contract:
        abi = self._load_abi(abi_filename)
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(address),
            abi=abi,
        )
