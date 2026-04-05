"""
统一日志模块

所有模块通过 logging.getLogger(__name__) 获取 logger，
本模块负责配置 root logger 的格式和输出目标。

使用方式：
    from utils.logger import setup_logger
    setup_logger(config.log)

    # 之后在任何模块里：
    import logging
    logger = logging.getLogger(__name__)
    logger.info("套利机会出现！")
"""

import logging
import os
from logging.handlers import RotatingFileHandler

from utils.config import LogConfig


def setup_logger(config: LogConfig) -> None:
    """
    配置 root logger：同时输出到控制台和文件。

    参数：
        config: LogConfig 实例，包含日志级别、文件路径等

    日志格式示例：
        2026-04-04 12:00:00 | INFO     | utils.web3_utils | 已连接 Optimism Sepolia
        2026-04-04 12:00:01 | WARNING  | bot.dex_arbitrage | Gas 价格过高，跳过本次套利

    文件轮转：
        文件超过 max_size（默认 10MB）时自动轮转，保留 backup_count 个旧文件。
        例如 bot.log → bot.log.1 → bot.log.2 → ...
    """
    # 确保日志目录存在
    log_dir = os.path.dirname(config.file_path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    # 日志格式
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台输出
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    # 文件输出（轮转）
    file_handler = RotatingFileHandler(
        filename=config.file_path,
        maxBytes=config.max_size,
        backupCount=config.backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    # 配置 root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, config.level.upper(), logging.INFO))
    # 清除已有 handler，避免重复添加
    root_logger.handlers.clear()
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)
