"""
配置管理模块

从 .env 文件加载所有配置，做类型转换和验证。
其他模块统一通过 Config.from_env() 获取配置，不要直接读 os.environ。
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


# ============================================================
# 子配置：每个 dataclass 对应一组相关配置
# ============================================================


@dataclass(frozen=True)
class OptimismConfig:
    """Optimism 链的连接和合约配置"""

    # RPC 连接
    rpc_http: str
    rpc_ws: str
    chain_id: int

    # Uniswap V3 合约
    uniswap_v3_quoter: str
    uniswap_v3_router: str
    uniswap_v3_factory: str

    # Velodrome V2 合约
    velodrome_router: str
    velodrome_factory: str

    # 代币地址
    weth: str
    usdc: str
    op: str
    usdt: str

    # 套利合约地址（部署后填入，为空表示 dry-run 模式）
    arbitrage_contract: str = ""


@dataclass(frozen=True)
class StrategyConfig:
    """套利策略参数"""

    min_profit_threshold: float  # 最小利润阈值，0.003 = 0.3%
    max_slippage: float  # 最大滑点，0.003 = 0.3%
    max_trade_amount: float  # 最大单笔交易金额 (USDC)
    min_trade_amount: float  # 最小单笔交易金额 (USDC)
    max_gas_price: float  # 最大 Gas 价格 (Gwei)
    gas_price_strategy: str  # dynamic / fixed


@dataclass(frozen=True)
class NotificationConfig:
    """通知配置"""

    telegram_enabled: bool  # 是否启用 Telegram 通知
    telegram_bot_token: str  # Telegram Bot Token
    telegram_chat_id: str  # Telegram Chat ID
    notify_on_trade: bool  # 交易执行时通知
    notify_on_error: bool  # 错误时通知
    notify_on_startup: bool  # 启动/关闭时通知
    stats_report_interval: int  # 统计报告间隔（秒），0 表示不发送


@dataclass(frozen=True)
class LogConfig:
    """日志配置"""

    level: str  # DEBUG / INFO / WARNING / ERROR
    file_path: str
    max_size: int  # 字节，默认 10MB
    backup_count: int  # 保留几个轮转文件


# ============================================================
# 顶层配置
# ============================================================


class Config:
    """
    顶层配置类，聚合所有子配置。

    使用方式:
        config = Config.from_env()          # 加载 .env
        config = Config.from_env("test.env")  # 加载指定文件

        print(config.optimism.rpc_http)     # Optimism HTTP RPC URL
        print(config.strategy.min_profit_threshold)  # 0.003
    """

    def __init__(
        self,
        optimism: OptimismConfig,
        strategy: StrategyConfig,
        log: LogConfig,
        notification: NotificationConfig,
        private_key: str,
        wallet_address: str,
    ):
        self.optimism = optimism
        self.strategy = strategy
        self.log = log
        self.notification = notification
        self.private_key = private_key
        self.wallet_address = wallet_address

    @classmethod
    def from_env(cls, env_path: str = ".env") -> "Config":
        """
        从 .env 文件加载配置。

        流程：
        1. 用 python-dotenv 把 .env 的内容注入到 os.environ
        2. 从 os.environ 读取每个字段
        3. 做类型转换（str → float / int）
        4. 验证必填字段不为空、格式合法
        5. 返回 Config 实例

        如果缺少必填字段或格式不对，立刻抛 ValueError。
        """
        load_dotenv(env_path)

        optimism = OptimismConfig(
            rpc_http=_require("OPTIMISM_RPC_HTTP"),
            rpc_ws=_require("OPTIMISM_RPC_WS"),
            chain_id=int(_require("OPTIMISM_CHAIN_ID")),
            uniswap_v3_quoter=_require("UNISWAP_V3_QUOTER"),
            uniswap_v3_router=_require("UNISWAP_V3_ROUTER"),
            uniswap_v3_factory=_require("UNISWAP_V3_FACTORY"),
            velodrome_router=_require("VELODROME_ROUTER"),
            velodrome_factory=_require("VELODROME_FACTORY"),
            weth=_require("WETH_ADDRESS"),
            usdc=_require("USDC_ADDRESS"),
            op=_require("OP_ADDRESS"),
            usdt=_require("USDT_ADDRESS"),
            arbitrage_contract=os.getenv("ARBITRAGE_CONTRACT", ""),
        )

        strategy = StrategyConfig(
            min_profit_threshold=float(os.getenv("MIN_PROFIT_THRESHOLD", "0.003")),
            max_slippage=float(os.getenv("MAX_SLIPPAGE", "0.003")),
            max_trade_amount=float(os.getenv("MAX_TRADE_AMOUNT", "1000")),
            min_trade_amount=float(os.getenv("MIN_TRADE_AMOUNT", "10")),
            max_gas_price=float(os.getenv("MAX_GAS_PRICE", "0.5")),
            gas_price_strategy=os.getenv("GAS_PRICE_STRATEGY", "dynamic"),
        )

        log = LogConfig(
            level=os.getenv("LOG_LEVEL", "INFO"),
            file_path=os.getenv("LOG_FILE_PATH", "./logs/bot.log"),
            max_size=int(os.getenv("LOG_MAX_SIZE", "10485760")),
            backup_count=int(os.getenv("LOG_BACKUP_COUNT", "5")),
        )

        notification = NotificationConfig(
            telegram_enabled=os.getenv("TELEGRAM_ENABLED", "false").lower() == "true",
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            notify_on_trade=os.getenv("NOTIFY_ON_TRADE", "true").lower() == "true",
            notify_on_error=os.getenv("NOTIFY_ON_ERROR", "true").lower() == "true",
            notify_on_startup=os.getenv("NOTIFY_ON_STARTUP", "true").lower() == "true",
            stats_report_interval=int(os.getenv("STATS_REPORT_INTERVAL", "1800")),
        )

        config = cls(
            optimism=optimism,
            strategy=strategy,
            log=log,
            notification=notification,
            private_key=_require("PRIVATE_KEY"),
            wallet_address=_require("WALLET_ADDRESS"),
        )
        config._validate()
        return config

    def _validate(self) -> None:
        """
        验证配置合法性。

        检查内容：
        - RPC URL 格式（http 必须 https://，ws 必须 wss://）
        - 合约/钱包地址格式（0x 开头，42 位）
        - 策略参数范围（正数、合理区间）
        """
        # RPC URL 格式
        if not self.optimism.rpc_http.startswith("https://"):
            raise ValueError(
                f"OPTIMISM_RPC_HTTP 必须以 https:// 开头，当前值: {self.optimism.rpc_http}"
            )
        if not self.optimism.rpc_ws.startswith("wss://"):
            raise ValueError(
                f"OPTIMISM_RPC_WS 必须以 wss:// 开头，当前值: {self.optimism.rpc_ws}"
            )

        # 地址格式验证
        address_fields = {
            "UNISWAP_V3_QUOTER": self.optimism.uniswap_v3_quoter,
            "UNISWAP_V3_ROUTER": self.optimism.uniswap_v3_router,
            "UNISWAP_V3_FACTORY": self.optimism.uniswap_v3_factory,
            "VELODROME_ROUTER": self.optimism.velodrome_router,
            "VELODROME_FACTORY": self.optimism.velodrome_factory,
            "WETH_ADDRESS": self.optimism.weth,
            "USDC_ADDRESS": self.optimism.usdc,
            "OP_ADDRESS": self.optimism.op,
            "USDT_ADDRESS": self.optimism.usdt,
            "WALLET_ADDRESS": self.wallet_address,
        }
        for name, addr in address_fields.items():
            _validate_address(name, addr)

        # 策略参数范围
        if self.strategy.min_profit_threshold <= 0:
            raise ValueError("MIN_PROFIT_THRESHOLD 必须大于 0")
        if self.strategy.max_slippage <= 0:
            raise ValueError("MAX_SLIPPAGE 必须大于 0")
        if self.strategy.min_trade_amount >= self.strategy.max_trade_amount:
            raise ValueError("MIN_TRADE_AMOUNT 必须小于 MAX_TRADE_AMOUNT")

    def __repr__(self) -> str:
        """打印配置时隐藏私钥"""
        return (
            f"Config(\n"
            f"  chain_id={self.optimism.chain_id},\n"
            f"  rpc_http={self.optimism.rpc_http},\n"
            f"  wallet={self.wallet_address},\n"
            f"  private_key=***,\n"
            f"  profit_threshold={self.strategy.min_profit_threshold},\n"
            f"  max_trade={self.strategy.max_trade_amount}\n"
            f")"
        )


# ============================================================
# 辅助函数
# ============================================================


def _require(key: str) -> str:
    """
    从环境变量读取必填字段。

    如果字段不存在或为空，立刻抛 ValueError。
    这样做的好处是：程序启动时就发现配置问题，
    而不是运行到一半才因为空值崩溃。
    """
    value = os.getenv(key)
    if not value or value.strip() == "":
        raise ValueError(
            f"缺少必填环境变量: {key}，请在 .env 文件中配置"
        )
    return value.strip()


def _validate_address(name: str, address: str) -> None:
    """
    验证以太坊地址格式。

    合法地址：以 0x 开头，总长 42 个字符（0x + 40 个十六进制字符）。
    例如：0x4200000000000000000000000000000000000006
    """
    if not address.startswith("0x"):
        raise ValueError(f"{name} 必须以 0x 开头，当前值: {address}")
    if len(address) != 42:
        raise ValueError(f"{name} 长度必须为 42，当前长度: {len(address)}")
