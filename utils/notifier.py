"""
通知模块

通过 Telegram 发送机器人运行通知：
- 启动/关闭
- 交易执行（套利 / 三明治）
- 错误警报
- 定期统计报告

使用 aiohttp 直接调用 Telegram Bot API，不依赖 python-telegram-bot。
如果 Telegram 未配置，所有通知只写日志。

使用方式：
    notifier = Notifier(config.notification)
    await notifier.start()
    await notifier.send_trade("套利成功", profit=0.05, ...)
    await notifier.stop()
"""

import asyncio
import logging
import time
from typing import Optional

import aiohttp

from utils.config import NotificationConfig

logger = logging.getLogger(__name__)

# Telegram API 基础 URL
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class Notifier:
    """
    通知管理器。

    功能：
    1. Telegram 消息推送（异步、非阻塞）
    2. 消息队列 + 后台发送（不阻塞策略主流程）
    3. 发送失败只记日志，不影响机器人运行
    4. 未配置 Telegram 时退化为纯日志
    """

    def __init__(self, config: NotificationConfig):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._sender_task: Optional[asyncio.Task] = None
        self._running = False

        # 统计
        self.total_sent = 0
        self.total_failed = 0

    @property
    def telegram_enabled(self) -> bool:
        """Telegram 是否可用（启用 + token 和 chat_id 都不为空）"""
        return (
            self.config.telegram_enabled
            and bool(self.config.telegram_bot_token)
            and bool(self.config.telegram_chat_id)
        )

    async def start(self) -> None:
        """启动通知模块：创建 HTTP session + 后台发送任务"""
        if self.telegram_enabled:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
            self._running = True
            self._sender_task = asyncio.create_task(self._send_loop())
            logger.info("Telegram 通知已启用")
        else:
            logger.info("Telegram 未配置，通知仅写日志")

    async def stop(self) -> None:
        """停止通知模块：清空队列、关闭连接"""
        self._running = False

        if self._sender_task:
            # 放一个 None 哨兵让 _send_loop 退出
            await self._queue.put(None)
            try:
                await asyncio.wait_for(self._sender_task, timeout=5)
            except asyncio.TimeoutError:
                self._sender_task.cancel()
                try:
                    await self._sender_task
                except asyncio.CancelledError:
                    pass

        if self._session and not self._session.closed:
            await self._session.close()

        logger.info(
            "通知模块已关闭 (发送=%d, 失败=%d)",
            self.total_sent, self.total_failed,
        )

    # ============================================================
    # 公开发送接口
    # ============================================================

    async def notify_startup(self, mode: str, dry_run: bool) -> None:
        """机器人启动通知"""
        if not self.config.notify_on_startup:
            return
        msg = (
            "<b>🤖 MEV 机器人启动</b>\n\n"
            f"模式: {mode}\n"
            f"Dry Run: {'是' if dry_run else '否'}\n"
            f"时间: {_now()}"
        )
        await self._enqueue(msg)

    async def notify_shutdown(self, uptime: float, stats: dict) -> None:
        """机器人关闭通知"""
        if not self.config.notify_on_startup:
            return
        msg = (
            "<b>🛑 MEV 机器人关闭</b>\n\n"
            f"运行时长: {_format_duration(uptime)}\n"
            f"套利信号: {stats.get('arb_signals', 0)}\n"
            f"三明治信号: {stats.get('sandwich_signals', 0)}\n"
            f"交易执行: {stats.get('total_trades', 0)}\n"
            f"时间: {_now()}"
        )
        await self._enqueue(msg)

    async def notify_trade(
        self,
        trade_type: str,
        direction: str,
        token_in: str,
        token_out: str,
        amount: float,
        profit: float,
        gas_cost: float,
        tx_hash: str = "",
        dry_run: bool = True,
    ) -> None:
        """交易执行通知"""
        if not self.config.notify_on_trade:
            return

        status = "🧪 DRY RUN" if dry_run else "✅ 已执行"
        msg = (
            f"<b>💰 {trade_type}</b> [{status}]\n\n"
            f"方向: {direction}\n"
            f"Token: {_short_addr(token_in)} → {_short_addr(token_out)}\n"
            f"金额: {amount:.2f} USDC\n"
            f"预估利润: {profit:.4f} ETH\n"
            f"Gas 成本: {gas_cost:.6f} ETH\n"
        )
        if tx_hash:
            msg += f"TX: <code>{tx_hash}</code>\n"
        msg += f"时间: {_now()}"
        await self._enqueue(msg)

    async def notify_error(self, module: str, error: str) -> None:
        """错误警报"""
        if not self.config.notify_on_error:
            return
        msg = (
            "<b>⚠️ 错误警报</b>\n\n"
            f"模块: {module}\n"
            f"错误: <code>{_escape_html(error[:500])}</code>\n"
            f"时间: {_now()}"
        )
        await self._enqueue(msg)

    async def notify_stats(self, stats: dict) -> None:
        """定期统计报告"""
        msg = (
            "<b>📊 运行统计</b>\n\n"
            f"运行时长: {_format_duration(stats.get('uptime', 0))}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"套利: 信号={stats.get('arb_signals', 0)}, "
            f"执行={stats.get('arb_executions', 0)}, "
            f"跳过={stats.get('arb_skips', 0)}\n"
            f"三明治: 信号={stats.get('sandwich_signals', 0)}, "
            f"执行={stats.get('sandwich_executions', 0)}, "
            f"跳过={stats.get('sandwich_skips', 0)}\n"
            f"交易: 总计={stats.get('total_trades', 0)}, "
            f"成功={stats.get('successful_trades', 0)}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"区块: #{stats.get('current_block', 0)}\n"
            f"连接: {'正常' if stats.get('chain_healthy') else '异常'}\n"
            f"时间: {_now()}"
        )
        await self._enqueue(msg)

    # ============================================================
    # 内部实现
    # ============================================================

    async def _enqueue(self, message: str) -> None:
        """将消息加入发送队列。如果 Telegram 未启用，只写日志。"""
        # 总是写日志（去掉 HTML 标签）
        clean = message.replace("<b>", "").replace("</b>", "")
        clean = clean.replace("<code>", "").replace("</code>", "")
        logger.info("[通知] %s", clean.replace("\n", " | "))

        if self.telegram_enabled and self._running:
            await self._queue.put(message)

    async def _send_loop(self) -> None:
        """后台任务：从队列取消息并发送到 Telegram"""
        while self._running or not self._queue.empty():
            try:
                message = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue

            # None 哨兵表示退出
            if message is None:
                break

            await self._send_telegram(message)

    async def _send_telegram(self, message: str) -> bool:
        """发送一条消息到 Telegram"""
        if not self._session:
            return False

        url = TELEGRAM_API.format(token=self.config.telegram_bot_token)
        payload = {
            "chat_id": self.config.telegram_chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        try:
            async with self._session.post(url, json=payload) as resp:
                if resp.status == 200:
                    self.total_sent += 1
                    return True
                else:
                    body = await resp.text()
                    logger.warning(
                        "Telegram 发送失败: status=%d, body=%s",
                        resp.status, body[:200],
                    )
                    self.total_failed += 1
                    return False
        except Exception as e:
            logger.warning("Telegram 发送异常: %s", e)
            self.total_failed += 1
            return False

    def get_stats(self) -> dict:
        """返回通知模块统计"""
        return {
            "telegram_enabled": self.telegram_enabled,
            "total_sent": self.total_sent,
            "total_failed": self.total_failed,
            "queue_size": self._queue.qsize(),
        }


# ============================================================
# 辅助函数
# ============================================================


def _now() -> str:
    """当前时间字符串"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _format_duration(seconds: float) -> str:
    """将秒数格式化为可读时长"""
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    if seconds < 3600:
        return f"{seconds / 60:.0f} 分钟"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    return f"{hours} 小时 {minutes} 分钟"


def _short_addr(addr: str) -> str:
    """缩短地址显示"""
    if len(addr) >= 42:
        return f"{addr[:6]}...{addr[-4:]}"
    return addr


def _escape_html(text: str) -> str:
    """转义 HTML 特殊字符"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
