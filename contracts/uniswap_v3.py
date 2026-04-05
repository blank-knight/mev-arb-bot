"""
Uniswap V3 合约接口（Optimism）

封装 Uniswap V3 的三个合约调用：
- Factory: 查找交易对的 Pool 地址
- Pool: 获取 Swap 事件主题（用于 WebSocket 订阅）
- Quoter V2: 查询"如果现在用 X 个 tokenA 换 tokenB，能换多少？"

使用方式：
    uni = UniswapV3(conn.w3, config.optimism)
    price = uni.get_quote(token_in=USDC, token_out=WETH, amount_in=1000e6)
    pool_addr = uni.get_pool(WETH, USDC, fee=3000)
"""

import json
import logging
from pathlib import Path
from typing import Optional

from web3 import Web3
from web3.contract import Contract

from utils.config import OptimismConfig

logger = logging.getLogger(__name__)

# ABI 文件路径
ABI_DIR = Path(__file__).parent / "abis"

# Uniswap V3 标准手续费等级（百万分之一）
# 500 = 0.05%, 3000 = 0.3%, 10000 = 1%
FEE_TIERS = [500, 3000, 10000]


class UniswapV3:
    """
    Uniswap V3 合约交互封装。

    什么是 Uniswap V3？
    一个去中心化交易所（DEX），用户可以在上面用一种代币换另一种。
    价格由 AMM（自动做市商）算法根据池子里的资金比例自动计算。

    什么是 Quoter？
    一个只读合约，用来查询"如果现在交易，能换多少代币"。
    调用 Quoter 不花 Gas（因为是 view 函数的模拟调用），
    可以随便调，用来实时查价格。
    """

    def __init__(self, w3: Web3, config: OptimismConfig):
        self.w3 = w3
        self.config = config

        # 加载 ABI 并创建合约实例
        self.quoter = self._load_contract(
            config.uniswap_v3_quoter,
            "uniswap_v3_quoter_v2.json",
        )
        self.factory = self._load_contract(
            config.uniswap_v3_factory,
            "uniswap_v3_factory.json",
        )

        # 缓存 Pool 合约 ABI（用于查 Pool 信息）
        self._pool_abi = self._load_abi("uniswap_v3_pool.json")

        # Swap 事件的主题哈希（用于 WebSocket 订阅）
        # keccak256("Swap(address,address,int256,int256,uint160,uint128,int24)")
        self.swap_event_topic = Web3.keccak(
            text="Swap(address,address,int256,int256,uint160,uint128,int24)"
        ).hex()

    def get_pool(self, token_a: str, token_b: str, fee: int = 3000) -> Optional[str]:
        """
        查找两个代币的交易池地址。

        参数：
            token_a: 代币 A 地址
            token_b: 代币 B 地址
            fee: 手续费等级（默认 3000 = 0.3%）

        返回：
            Pool 合约地址，如果不存在返回 None

        什么是 Pool？
        每个交易对（如 WETH/USDC）在每个手续费等级上有一个独立的池子。
        流动性提供者（LP）往池子里存入代币，交易者从池子里换币。
        """
        pool_address = self.factory.functions.getPool(
            Web3.to_checksum_address(token_a),
            Web3.to_checksum_address(token_b),
            fee,
        ).call()

        # 地址全零表示池子不存在
        if pool_address == "0x0000000000000000000000000000000000000000":
            logger.warning(
                "Uniswap V3 池子不存在: %s/%s (fee=%d)", token_a[:10], token_b[:10], fee
            )
            return None

        logger.debug("Uniswap V3 Pool: %s (fee=%d)", pool_address, fee)
        return pool_address

    def get_pool_contract(self, pool_address: str) -> Contract:
        """根据 Pool 地址创建合约实例（用于读取 Pool 信息）"""
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(pool_address),
            abi=self._pool_abi,
        )

    def get_quote(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        fee: int = 3000,
    ) -> Optional[int]:
        """
        查询报价：用 amount_in 个 token_in 能换多少 token_out。

        参数：
            token_in: 卖出的代币地址
            token_out: 买入的代币地址
            amount_in: 卖出数量（最小单位，如 USDC 是 6 位小数，1 USDC = 1000000）
            fee: 手续费等级

        返回：
            能换到的 token_out 数量（最小单位），失败返回 None

        重要概念 - 代币精度：
            每个代币有不同的小数位数（decimals）。
            USDC = 6 位小数 → 1 USDC = 1_000_000（10^6）
            WETH = 18 位小数 → 1 WETH = 1_000_000_000_000_000_000（10^18）

            所以查"1000 USDC 能换多少 WETH"：
            amount_in = 1000 * 10**6 = 1_000_000_000
        """
        try:
            # Quoter V2 的 quoteExactInputSingle 函数
            # 虽然标记为 nonpayable，但我们用 .call() 调用（不发交易，只模拟）
            result = self.quoter.functions.quoteExactInputSingle(
                (
                    Web3.to_checksum_address(token_in),
                    Web3.to_checksum_address(token_out),
                    amount_in,
                    fee,
                    0,  # sqrtPriceLimitX96 = 0 表示不限价
                )
            ).call()

            amount_out = result[0]  # 能换到的数量
            gas_estimate = result[3]  # 预估 Gas 消耗

            logger.debug(
                "Uniswap V3 报价: %s → %s, in=%d, out=%d, gas=%d",
                token_in[:10],
                token_out[:10],
                amount_in,
                amount_out,
                gas_estimate,
            )
            return amount_out

        except Exception as e:
            logger.error("Uniswap V3 报价失败: %s", e)
            return None

    def get_price(
        self,
        token_in: str,
        token_out: str,
        amount_in_human: float,
        token_in_decimals: int = 6,
        token_out_decimals: int = 18,
        fee: int = 3000,
    ) -> Optional[float]:
        """
        查询人类可读的价格。

        参数：
            token_in: 卖出代币地址
            token_out: 买入代币地址
            amount_in_human: 卖出数量（人类可读，如 1000.0 表示 1000 USDC）
            token_in_decimals: 卖出代币精度（USDC=6, WETH=18）
            token_out_decimals: 买入代币精度
            fee: 手续费等级

        返回：
            换算后的人类可读数量，如 0.333 表示能换到 0.333 WETH

        使用示例：
            # 1000 USDC 能换多少 WETH？
            weth_amount = uni.get_price(
                token_in=USDC, token_out=WETH,
                amount_in_human=1000.0,
                token_in_decimals=6, token_out_decimals=18
            )
            # → 0.333 (0.333 WETH)
        """
        amount_in_raw = int(amount_in_human * (10**token_in_decimals))
        amount_out_raw = self.get_quote(token_in, token_out, amount_in_raw, fee)

        if amount_out_raw is None:
            return None

        return amount_out_raw / (10**token_out_decimals)

    def find_best_fee_tier(
        self, token_in: str, token_out: str, amount_in: int
    ) -> Optional[tuple[int, int]]:
        """
        在所有手续费等级中找到最优报价。

        返回：(最佳手续费等级, 最多能换到的数量)

        为什么有多个手续费等级？
        Uniswap V3 允许 LP 选择不同的手续费：
        - 0.05% (500): 稳定币对（USDC/USDT），价格波动小
        - 0.30% (3000): 常规对（WETH/USDC），最常见
        - 1.00% (10000): 冷门币对，价格波动大

        不同等级的池子流动性不同，同样的交易在不同池子报价可能不同。
        """
        best_fee = None
        best_amount = 0

        for fee in FEE_TIERS:
            amount_out = self.get_quote(token_in, token_out, amount_in, fee)
            if amount_out is not None and amount_out > best_amount:
                best_amount = amount_out
                best_fee = fee

        if best_fee is None:
            return None

        logger.info("Uniswap V3 最佳费率: %d (出价: %d)", best_fee, best_amount)
        return best_fee, best_amount

    # ============================================================
    # 内部方法
    # ============================================================

    def _load_abi(self, filename: str) -> list:
        """从 JSON 文件加载合约 ABI"""
        abi_path = ABI_DIR / filename
        with open(abi_path) as f:
            return json.load(f)

    def _load_contract(self, address: str, abi_filename: str) -> Contract:
        """加载合约 ABI 并创建 Web3 合约实例"""
        abi = self._load_abi(abi_filename)
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(address),
            abi=abi,
        )
