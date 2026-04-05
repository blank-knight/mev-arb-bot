"""
交易执行器

接收 DexArbitrage 的交易决策，通过 Solidity 原子合约执行链上套利。

原子合约保证：买入+卖出+利润检查在同一笔交易内完成。
如果利润不够 → 整笔交易自动 revert，只损失 Gas 费。

使用方式：
    executor = TransactionExecutor(conn, uni, velo, config)
    strategy.on_trade = executor.execute
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from web3 import Web3

from bot.dex_arbitrage import TradeDecision
from contracts.uniswap_v3 import UniswapV3
from contracts.velodrome import Velodrome
from utils.config import Config
from utils.web3_utils import ChainConnection

logger = logging.getLogger(__name__)

# ABI 路径
ABI_DIR = Path(__file__).parent.parent / "contracts" / "abis"
ERC20_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
        "stateMutability": "view",
    },
]


@dataclass
class TradeResult:
    """交易执行结果"""

    timestamp: float
    success: bool
    decision: TradeDecision
    tx_hash: Optional[str] = None
    gas_used: int = 0
    error: Optional[str] = None


class TransactionExecutor:
    """
    交易执行器：通过 Solidity 原子合约执行套利。

    流程：
    1. 检查 tokenIn allowance，不够则 approve
    2. 根据 buy_dex/sell_dex 选择 executeArbitrage 或 executeArbitrageReverse
    3. 构造并签名交易
    4. 发送交易，等待确认
    5. 返回执行结果

    安全保证：
    - 合约内部会检查利润，不够自动 revert
    - 最坏情况只损失 Gas 费（Optimism 上约 $0.001）
    - 没有部署合约地址时自动降级为 dry-run
    """

    def __init__(
        self,
        conn: ChainConnection,
        uniswap: UniswapV3,
        velodrome: Velodrome,
        config: Config,
    ):
        self.conn = conn
        self.uniswap = uniswap
        self.velodrome = velodrome
        self.config = config
        self.w3 = conn.w3

        # 加载合约
        self.contract = None
        contract_addr = config.optimism.arbitrage_contract
        if contract_addr and len(contract_addr) == 42:
            abi_path = ABI_DIR / "arbitrage_executor.json"
            if abi_path.exists():
                with open(abi_path) as f:
                    abi = json.load(f)
                self.contract = self.w3.eth.contract(
                    address=Web3.to_checksum_address(contract_addr),
                    abi=abi,
                )
                logger.info("套利合约已加载: %s", contract_addr)
            else:
                logger.warning("找不到合约 ABI: %s", abi_path)

        # 执行历史（上限 200 条）
        self.results: list[TradeResult] = []
        self._max_results = 200
        self.total_trades = 0
        self.successful_trades = 0

        # nonce 锁：防止并发交易使用相同 nonce
        self._nonce_lock = asyncio.Lock()
        # 交易重试次数
        self._max_retries = 2

    async def execute(self, decision: TradeDecision) -> TradeResult:
        """
        执行一个交易决策。

        如果合约地址未配置，降级为 dry-run（只记录日志）。
        支持重试（网络超时等瞬态错误），通过 nonce lock 防止并发冲突。
        """
        self.total_trades += 1
        now = time.time()

        logger.info(
            "执行交易 #%d: %s 买 → %s 卖, 金额=%d",
            self.total_trades,
            decision.buy_dex,
            decision.sell_dex,
            decision.amount_in,
        )

        # 没有合约 → dry-run
        if self.contract is None:
            return self._dry_run(decision, now)

        # 带重试的链上执行
        last_error = None
        for attempt in range(1, self._max_retries + 1):
            try:
                async with self._nonce_lock:
                    result = await self._execute_onchain(decision, now)
                if result.success:
                    self.successful_trades += 1
                self._append_result(result)
                return result
            except Exception as e:
                last_error = e
                if attempt < self._max_retries:
                    logger.warning(
                        "交易执行失败 (尝试 %d/%d): %s，重试中...",
                        attempt, self._max_retries, e,
                    )
                    await asyncio.sleep(0.5)
                else:
                    logger.error(
                        "交易执行失败 (尝试 %d/%d): %s",
                        attempt, self._max_retries, e,
                    )

        result = TradeResult(
            timestamp=now,
            success=False,
            decision=decision,
            error=str(last_error),
        )
        self._append_result(result)
        return result

    async def _execute_onchain(
        self, decision: TradeDecision, timestamp: float
    ) -> TradeResult:
        """
        链上执行套利。

        步骤：
        1. 确保 tokenIn 已 approve 给合约
        2. 构造合约调用
        3. 估算 Gas
        4. 签名并发送
        5. 等待确认
        """
        account = self.w3.eth.account.from_key(self.config.private_key)
        sender = account.address
        contract_addr = self.contract.address

        # 1. 检查并 approve tokenIn
        await self._ensure_allowance(
            decision.token_in, sender, contract_addr, decision.amount_in
        )

        # 2. 构造合约调用
        # 默认参数
        uniswap_fee = 3000  # 0.3% fee tier
        velo_stable = False  # volatile pool
        min_profit = 0  # 合约内检查，这里设 0（利润检查在策略层已做过）

        if decision.buy_dex == "velodrome":
            # Velodrome 买 → Uniswap 卖
            tx_func = self.contract.functions.executeArbitrage(
                Web3.to_checksum_address(decision.token_in),
                Web3.to_checksum_address(decision.token_out),
                decision.amount_in,
                uniswap_fee,
                velo_stable,
                min_profit,
            )
        else:
            # Uniswap 买 → Velodrome 卖
            tx_func = self.contract.functions.executeArbitrageReverse(
                Web3.to_checksum_address(decision.token_in),
                Web3.to_checksum_address(decision.token_out),
                decision.amount_in,
                uniswap_fee,
                velo_stable,
                min_profit,
            )

        # 3. 估算 Gas
        gas_estimate = tx_func.estimate_gas({"from": sender})
        gas_limit = int(gas_estimate * 1.2)  # 20% 余量

        # 4. 构造、签名、发送交易
        nonce = self.w3.eth.get_transaction_count(sender)
        gas_price = self.w3.eth.gas_price

        tx = tx_func.build_transaction(
            {
                "from": sender,
                "nonce": nonce,
                "gas": gas_limit,
                "gasPrice": gas_price,
                "chainId": self.config.optimism.chain_id,
            }
        )

        signed = account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        logger.info("交易已发送: %s", tx_hash.hex())

        # 5. 等待确认
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        success = receipt["status"] == 1
        if success:
            logger.info(
                "套利成功! tx=%s, gas_used=%d",
                tx_hash.hex(), receipt["gasUsed"],
            )
        else:
            logger.warning(
                "套利 revert（利润不足）: tx=%s, gas_used=%d",
                tx_hash.hex(), receipt["gasUsed"],
            )

        return TradeResult(
            timestamp=timestamp,
            success=success,
            decision=decision,
            tx_hash=tx_hash.hex(),
            gas_used=receipt["gasUsed"],
            error=None if success else "Transaction reverted",
        )

    async def _ensure_allowance(
        self,
        token_addr: str,
        owner: str,
        spender: str,
        amount: int,
    ):
        """检查 ERC20 allowance，不够则 approve max。"""
        token = self.w3.eth.contract(
            address=Web3.to_checksum_address(token_addr),
            abi=ERC20_ABI,
        )
        current = token.functions.allowance(
            Web3.to_checksum_address(owner),
            Web3.to_checksum_address(spender),
        ).call()

        if current >= amount:
            return

        logger.info("Approve %s → %s (max)", token_addr[:10], spender[:10])

        max_uint = 2**256 - 1
        account = self.w3.eth.account.from_key(self.config.private_key)
        nonce = self.w3.eth.get_transaction_count(owner)

        tx = token.functions.approve(
            Web3.to_checksum_address(spender), max_uint
        ).build_transaction(
            {
                "from": owner,
                "nonce": nonce,
                "gas": 100_000,
                "gasPrice": self.w3.eth.gas_price,
                "chainId": self.config.optimism.chain_id,
            }
        )

        signed = account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
        logger.info("Approve 完成: %s", tx_hash.hex())

    def _dry_run(self, decision: TradeDecision, timestamp: float) -> TradeResult:
        """Dry-run 模式：只记录，不执行。"""
        result = TradeResult(
            timestamp=timestamp,
            success=True,
            decision=decision,
            error="dry-run 模式（未配置合约地址）",
        )

        logger.info(
            "[DRY RUN] 交易 #%d: 买 %s, 卖 %s, 预估净利=$%.4f",
            self.total_trades,
            decision.buy_dex,
            decision.sell_dex,
            decision.net_profit_usd,
        )

        self._append_result(result)
        return result

    def _append_result(self, result: TradeResult) -> None:
        """保存执行结果，超过上限时丢弃最早的"""
        self.results.append(result)
        if len(self.results) > self._max_results:
            self.results = self.results[-self._max_results:]

    def get_stats(self) -> dict:
        """获取执行器统计"""
        return {
            "total_trades": self.total_trades,
            "successful_trades": self.successful_trades,
            "failed_trades": self.total_trades - self.successful_trades,
            "success_rate": (
                self.successful_trades / self.total_trades
                if self.total_trades > 0
                else 0
            ),
            "has_contract": self.contract is not None,
        }
