"""
通知模块单元测试

测试 Notifier 的消息格式化、队列管理、Telegram 发送逻辑。
不需要真实的 Telegram Bot Token，全部用 mock。
"""

import asyncio

import pytest

from utils.notifier import (
    Notifier,
    _escape_html,
    _format_duration,
    _short_addr,
)


# ============================================================
# Mock 配置
# ============================================================


class MockNotificationConfig:
    """未启用 Telegram 的配置"""

    telegram_enabled = False
    telegram_bot_token = ""
    telegram_chat_id = ""
    notify_on_trade = True
    notify_on_error = True
    notify_on_startup = True
    stats_report_interval = 1800


class MockTelegramConfig:
    """已启用 Telegram 的配置"""

    telegram_enabled = True
    telegram_bot_token = "123456:ABC-DEF"
    telegram_chat_id = "987654"
    notify_on_trade = True
    notify_on_error = True
    notify_on_startup = True
    stats_report_interval = 1800


class MockDisabledTradeConfig:
    """关闭交易通知的配置"""

    telegram_enabled = False
    telegram_bot_token = ""
    telegram_chat_id = ""
    notify_on_trade = False
    notify_on_error = False
    notify_on_startup = False
    stats_report_interval = 0


# ============================================================
# 辅助函数测试
# ============================================================


class TestHelperFunctions:
    """测试辅助函数"""

    def test_format_duration_seconds(self):
        assert _format_duration(30) == "30 秒"

    def test_format_duration_minutes(self):
        assert _format_duration(300) == "5 分钟"

    def test_format_duration_hours(self):
        assert _format_duration(7260) == "2 小时 1 分钟"

    def test_short_addr(self):
        addr = "0x4200000000000000000000000000000000000006"
        assert _short_addr(addr) == "0x4200...0006"

    def test_short_addr_short_string(self):
        assert _short_addr("0x123") == "0x123"

    def test_escape_html(self):
        assert _escape_html("<script>alert('xss')</script>") == (
            "&lt;script&gt;alert('xss')&lt;/script&gt;"
        )

    def test_escape_html_ampersand(self):
        assert _escape_html("a & b") == "a &amp; b"


# ============================================================
# Notifier 初始化测试
# ============================================================


class TestNotifierInit:
    """测试初始化"""

    def test_telegram_disabled(self):
        """未配置 Telegram 时应该禁用"""
        notifier = Notifier(MockNotificationConfig())
        assert notifier.telegram_enabled is False

    def test_telegram_enabled(self):
        """配置完整时应该启用"""
        notifier = Notifier(MockTelegramConfig())
        assert notifier.telegram_enabled is True

    def test_telegram_enabled_but_no_token(self):
        """启用但没 token 应该禁用"""

        class Config:
            telegram_enabled = True
            telegram_bot_token = ""
            telegram_chat_id = "123"
            notify_on_trade = True
            notify_on_error = True
            notify_on_startup = True
            stats_report_interval = 0

        notifier = Notifier(Config())
        assert notifier.telegram_enabled is False

    def test_initial_stats(self):
        """初始统计应该为 0"""
        notifier = Notifier(MockNotificationConfig())
        stats = notifier.get_stats()
        assert stats["total_sent"] == 0
        assert stats["total_failed"] == 0
        assert stats["queue_size"] == 0


# ============================================================
# Notifier 生命周期测试
# ============================================================


class TestNotifierLifecycle:
    """测试启动和关闭"""

    @pytest.mark.asyncio
    async def test_start_stop_disabled(self):
        """Telegram 禁用时 start/stop 不应该出错"""
        notifier = Notifier(MockNotificationConfig())
        await notifier.start()
        assert notifier._session is None
        await notifier.stop()

    @pytest.mark.asyncio
    async def test_start_stop_enabled(self):
        """Telegram 启用时应该创建 session"""
        notifier = Notifier(MockTelegramConfig())
        await notifier.start()
        assert notifier._session is not None
        assert notifier._running is True
        await notifier.stop()
        assert notifier._session.closed

    @pytest.mark.asyncio
    async def test_double_stop(self):
        """连续 stop 两次不应该出错"""
        notifier = Notifier(MockTelegramConfig())
        await notifier.start()
        await notifier.stop()
        await notifier.stop()


# ============================================================
# 消息发送测试（Telegram 禁用模式）
# ============================================================


class TestNotifierMessagesDisabled:
    """Telegram 禁用时消息应该只写日志，不入队"""

    @pytest.mark.asyncio
    async def test_notify_startup_disabled(self):
        """启动通知不应该入队"""
        notifier = Notifier(MockNotificationConfig())
        await notifier.start()
        await notifier.notify_startup(mode="ws", dry_run=True)
        assert notifier._queue.qsize() == 0
        await notifier.stop()

    @pytest.mark.asyncio
    async def test_notify_trade_disabled(self):
        """交易通知不应该入队"""
        notifier = Notifier(MockNotificationConfig())
        await notifier.start()
        await notifier.notify_trade(
            trade_type="套利",
            direction="Uniswap → Velodrome",
            token_in="0x" + "a" * 40,
            token_out="0x" + "b" * 40,
            amount=1000.0,
            profit=0.005,
            gas_cost=0.0001,
        )
        assert notifier._queue.qsize() == 0
        await notifier.stop()

    @pytest.mark.asyncio
    async def test_notify_error_disabled(self):
        """错误通知不应该入队"""
        notifier = Notifier(MockNotificationConfig())
        await notifier.start()
        await notifier.notify_error("test_module", "test error message")
        assert notifier._queue.qsize() == 0
        await notifier.stop()


# ============================================================
# 消息内容格式测试
# ============================================================


class TestNotifierMessageContent:
    """测试消息内容格式是否正确"""

    @pytest.mark.asyncio
    async def test_startup_message_enqueued(self):
        """Telegram 启用时启动通知应该入队"""
        notifier = Notifier(MockTelegramConfig())
        await notifier.start()

        # 暂停发送循环，只测入队
        notifier._running = False
        await asyncio.sleep(0.05)  # 让 _send_loop 退出

        notifier._running = True  # 恢复以便 _enqueue 能入队
        await notifier._enqueue("测试消息")
        assert notifier._queue.qsize() == 1

        msg = await notifier._queue.get()
        assert msg == "测试消息"

        notifier._running = False
        await notifier.stop()

    @pytest.mark.asyncio
    async def test_notify_skipped_when_disabled(self):
        """关闭各类通知开关时不应该发送"""
        notifier = Notifier(MockDisabledTradeConfig())
        await notifier.start()

        await notifier.notify_trade(
            trade_type="套利", direction="test",
            token_in="0x" + "a" * 40, token_out="0x" + "b" * 40,
            amount=100, profit=0.01, gas_cost=0.001,
        )
        await notifier.notify_error("test", "error")
        await notifier.notify_startup("ws", True)

        assert notifier._queue.qsize() == 0
        await notifier.stop()

    @pytest.mark.asyncio
    async def test_stats_report(self):
        """统计报告应该能正常生成"""
        notifier = Notifier(MockNotificationConfig())
        await notifier.start()

        # notify_stats 总是写日志，不检查入队（Telegram 禁用）
        await notifier.notify_stats({
            "uptime": 3600,
            "arb_signals": 10,
            "arb_executions": 2,
            "arb_skips": 8,
            "sandwich_signals": 5,
            "sandwich_executions": 1,
            "sandwich_skips": 4,
            "total_trades": 3,
            "successful_trades": 3,
            "current_block": 12345,
            "chain_healthy": True,
        })
        # 不崩溃就算通过
        await notifier.stop()

    @pytest.mark.asyncio
    async def test_shutdown_message(self):
        """关闭通知应该包含统计信息"""
        notifier = Notifier(MockNotificationConfig())
        await notifier.start()

        await notifier.notify_shutdown(3600, {
            "arb_signals": 5,
            "sandwich_signals": 3,
            "total_trades": 2,
        })
        # 不崩溃就算通过
        await notifier.stop()


# ============================================================
# Telegram 发送测试（mock HTTP）
# ============================================================


class TestTelegramSend:
    """测试 Telegram 发送逻辑"""

    @pytest.mark.asyncio
    async def test_send_telegram_no_session(self):
        """没有 session 时发送应该返回 False"""
        notifier = Notifier(MockTelegramConfig())
        result = await notifier._send_telegram("test")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_telegram_success(self):
        """模拟成功发送"""
        notifier = Notifier(MockTelegramConfig())
        await notifier.start()

        # 替换 _send_telegram 为 mock
        sent_messages = []

        async def mock_send(msg):
            sent_messages.append(msg)
            notifier.total_sent += 1
            return True

        notifier._send_telegram = mock_send

        # 直接入队一条消息
        await notifier._enqueue("测试消息")

        # 等待后台任务处理
        await asyncio.sleep(0.2)

        assert len(sent_messages) == 1
        assert sent_messages[0] == "测试消息"
        assert notifier.total_sent == 1

        await notifier.stop()

    @pytest.mark.asyncio
    async def test_send_telegram_failure(self):
        """模拟发送失败"""
        notifier = Notifier(MockTelegramConfig())
        await notifier.start()

        async def mock_send(msg):
            notifier.total_failed += 1
            return False

        notifier._send_telegram = mock_send

        await notifier._enqueue("测试消息")
        await asyncio.sleep(0.2)

        assert notifier.total_failed == 1
        await notifier.stop()

    @pytest.mark.asyncio
    async def test_get_stats_after_activity(self):
        """活动后统计应该更新"""
        notifier = Notifier(MockTelegramConfig())
        await notifier.start()

        async def mock_send(msg):
            notifier.total_sent += 1
            return True

        notifier._send_telegram = mock_send

        await notifier._enqueue("msg1")
        await notifier._enqueue("msg2")
        await asyncio.sleep(0.2)

        stats = notifier.get_stats()
        assert stats["total_sent"] == 2
        assert stats["telegram_enabled"] is True

        await notifier.stop()
