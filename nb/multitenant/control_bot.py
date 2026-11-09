"""Private-chat control bot. It never handles or forwards channel messages.
Optimized with:
1. Removed 5-minute expiration on magic links and login attempts.
2. One-click Phone Share button (KeyboardButtonRequestPhone) for single-phone login.
3. Automatic OTP recognition (handles forwarded/spaced codes like '1 2 3 4 5' without needing /code).
4. Direct StringSession import (/session) and QR deep-link support.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

import qrcode
from telethon import Button, TelegramClient, events
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.sessions import StringSession
from telethon.tl.types import KeyboardButtonRequestPhone, KeyboardButtonWebView, ReplyKeyboardMarkup, KeyboardButtonRow

from .repository import MongoRepository
from .security import SessionCipher, get_telegram_proxy
from .settings import Settings


@dataclass
class LoginAttempt:
    client: TelegramClient
    created_at: float
    phone: str = ""
    waiting_password: bool = False


def _get_proxy():
    return get_telegram_proxy()


class ControlBot:
    def __init__(self, settings: Settings, repository: MongoRepository) -> None:
        self.settings = settings
        self.repo = repository
        self.cipher = SessionCipher(settings.session_encryption_key)
        self.bot = TelegramClient(
            StringSession(),
            settings.api_id,
            settings.api_hash,
            proxy=_get_proxy(),
        )
        self.attempts: dict[int, LoginAttempt] = {}
        self.rate_limits: dict[int, list[float]] = {}

    async def run(self) -> None:
        self.repo.ensure_indexes()
        await self.bot.start(bot_token=self.settings.bot_token)
        self._register_handlers()
        logging.info("Control bot started; only private command handlers are registered")
        notifier = asyncio.create_task(self._notify_status_changes())
        try:
            await self.bot.run_until_disconnected()
        finally:
            notifier.cancel()
            await asyncio.gather(notifier, return_exceptions=True)

    def _register_handlers(self) -> None:
        private = lambda event: bool(event.is_private)
        self.bot.add_event_handler(self.start, events.NewMessage(pattern=r"^/start$", func=private))
        self.bot.add_event_handler(self.login, events.NewMessage(pattern=r"^/login$", func=private))
        self.bot.add_event_handler(self.qr_login_cmd, events.NewMessage(pattern=r"^/qr$", func=private))
        self.bot.add_event_handler(self.phone, events.NewMessage(pattern=r"^/phone(?:\s+(.+))?$", func=private))
        self.bot.add_event_handler(self.session_import, events.NewMessage(pattern=r"^/session(?:\s+(.+))?$", func=private))
        self.bot.add_event_handler(self.app, events.NewMessage(pattern=r"^/app$", func=private))
        self.bot.add_event_handler(self.web, events.NewMessage(pattern=r"^/web$", func=private))
        self.bot.add_event_handler(self.status, events.NewMessage(pattern=r"^/status$", func=private))
        self.bot.add_event_handler(self.logout, events.NewMessage(pattern=r"^/logout$", func=private))
        # 处理一键共享手机号 (Contact Share) 与免前缀验证码/密码输入
        self.bot.add_event_handler(self.on_contact_or_smart_input, events.NewMessage(func=private))

    async def start(self, event) -> None:
        sender = await event.get_sender()
        name = " ".join(part for part in [sender.first_name, sender.last_name] if part)
        self.repo.upsert_tenant(sender.id, username=sender.username or "", display_name=name)
        buttons = []
        if self.settings.public_base_url:
            url = f"{self.settings.public_base_url}/miniapp"
            buttons.append([KeyboardButtonWebView("🚀 打开 Liquid Glass 控制台 (Mini App)", url)])
        # 注意：Telethon 不允许同一条消息混用内联按钮（WebView）和普通键盘按钮
        # （request_phone），混用会抛 ValueError 导致整条回复发不出去，所以这里只保留 WebView；
        # 一键共享号码请走 /login 或 /phone。
        await event.respond(
            "✨ **欢迎使用 NB 多租户转发系统 · Liquid Glass 旗舰版**\n\n"
            "**推荐单手机登录方式（无需扫码）：**\n"
            "1️⃣ 发送 `/login` 打开登录菜单，点「📱 一键共享本机号码」，或直接发送 `/phone +国家码手机号`\n"
            "2️⃣ 收到验证码后，直接输入带空格数字（如 `1 2 3 4 5`)\n"
            "3️⃣ 也可在 `/web` 网页端可视化输入手机号验证码或粘贴 `StringSession`\n\n"
            "**常用命令：**\n"
            "`/login` 打开多种登录方式菜单\n"
            "`/session <StringSession>` 直接导入已有会话字符串（零扫码零验证码）\n"
            "`/app` 打开 Mini App 控制台\n"
            "`/web` 打开 Web 后台入口\n"
            "`/status` 查看账号与任务状态\n"
            "`/logout` 解绑并删除全部数据",
            buttons=buttons,
        )

    def _allow_login(self, tenant_id: int) -> bool:
        now = time.monotonic()
        attempts = [value for value in self.rate_limits.get(tenant_id, []) if now - value < 900]
        if len(attempts) >= 15:
            self.rate_limits[tenant_id] = attempts
            return False
        attempts.append(now)
        self.rate_limits[tenant_id] = attempts
        return True

    async def _new_client(self) -> TelegramClient:
        client = TelegramClient(
            StringSession(),
            self.settings.api_id,
            self.settings.api_hash,
            proxy=_get_proxy(),
        )
        await client.connect()
        return client

    async def _replace_attempt(self, tenant_id: int, attempt: LoginAttempt) -> None:
        previous = self.attempts.pop(tenant_id, None)
        if previous:
            await previous.client.disconnect()
        self.attempts[tenant_id] = attempt

    async def login(self, event) -> None:
        """提供单手机友好的多模式登录选择，不再强制弹出难以单机扫描的二维码。"""
        tenant_id = int(event.sender_id)
        if self.repo.get_account(tenant_id):
            await event.respond("当前账号已经绑定。如需重绑，请先使用 /logout。")
            return
        # 注意：这里不再生成免密登录链接——Telethon 不允许同一条消息混用普通键盘按钮
        # （request_phone）和内联按钮（Button.url），混用会抛 ValueError 导致整条回复
        # 发不出去；同时免密链接本身谁拿到都能直接登录，已按决定停用。
        msg = (
            "🔐 **请选择最适合您的登录绑定方式（单手机免扫码优化）：**\n\n"
            "• **方式一（单手机最稳）：一键共享本机号码**\n"
            "  点击底部键盘 **「📱 一键共享本机号码」** 或发送 `/phone +8613800000000`\n\n"
            "• **方式二（网页可视化向导）：Web 后台登录**\n"
            "  发送 `/web` 进入网页端，在玻璃拟态面板中直接输入手机号/验证码或导入 Session\n\n"
            "• **方式三（最极速零风控）：StringSession 直接导入**\n"
            "  发送 `/session 1BVtsO...` 瞬间完成绑定\n\n"
            "• **方式四（双设备扫码 + 自动刷新）：**\n"
            "  如果您身边有另一台设备，请发送 `/qr` 获取自动刷新的二维码与 `tg://login` 链接。"
        )
        buttons = [[Button.request_phone("📱 一键共享本机号码", resize=True, single_use=True)]]
        await event.respond(msg, buttons=buttons)

    async def qr_login_cmd(self, event) -> None:
        """支持自动续期循环刷新的二维码 + tg://login Deep Link 登录。"""
        tenant_id = int(event.sender_id)
        if not self._allow_login(tenant_id):
            await event.respond("登录尝试过于频繁，请稍后再试。")
            return
        if self.repo.get_account(tenant_id):
            await event.respond("当前账号已经绑定。如需重绑，请先使用 /logout。")
            return
        client = await self._new_client()
        await self._replace_attempt(tenant_id, LoginAttempt(client=client, created_at=time.monotonic()))
        asyncio.create_task(self._qr_loop(tenant_id, client, event.chat_id))

    async def _qr_loop(self, tenant_id: int, client: TelegramClient, chat_id: int) -> None:
        """自动刷新 QR token（最多持续 5 轮），避免 Telegram 服务端 30 秒令牌过期导致扫不出。"""
        try:
            qr_login = await client.qr_login()
            for round_idx in range(5):
                attempt = self.attempts.get(tenant_id)
                if attempt is None or attempt.client is not client:
                    return
                if round_idx > 0:
                    await qr_login.recreate()
                image = qrcode.make(qr_login.url)
                buffer = io.BytesIO()
                buffer.name = "telegram-login.png"
                image.save(buffer, format="PNG")
                buffer.seek(0)
                await self.bot.send_message(
                    chat_id,
                    f"📷 **二维码登录（第 {round_idx + 1}/5 次自动保鲜）**\n"
                    f"• 另一台设备：打开 Telegram → 设置 → 设备 → 扫描二维码\n"
                    f"• 桌面端/跨端直连：`{qr_login.url}`\n"
                    f"• 单手机不便扫码？直接发送 `/phone +国家码手机号`",
                    file=buffer,
                )
                try:
                    await qr_login.wait(timeout=35)
                    await self._complete_login(tenant_id, client, chat_id)
                    return
                except asyncio.TimeoutError:
                    continue
            await self.bot.send_message(chat_id, "二维码轮询已结束，推荐使用单手机 `/phone +国家码手机号` 登录。")
            await self._discard_attempt(tenant_id, client)
        except SessionPasswordNeededError:
            attempt = self.attempts.get(tenant_id)
            if attempt and attempt.client is client:
                attempt.waiting_password = True
                await self.bot.send_message(chat_id, "🔐 该账号启用了两步验证，请直接发送您的两步验证密码。")
        except Exception as exc:
            logging.warning("QR login failed for tenant=%s: %s", tenant_id, type(exc).__name__)
            await self.bot.send_message(chat_id, "扫码登录中断，请使用单手机 `/phone +国家码手机号` 登录。")
            await self._discard_attempt(tenant_id, client)

    async def _start_phone_login(self, event, phone_raw: str) -> None:
        tenant_id = int(event.sender_id)
        phone = re.sub(r"[\s()-]", "", phone_raw)
        if not phone.startswith("+"):
            phone = "+" + phone
        if not re.fullmatch(r"\+\d{7,15}", phone):
            await event.respond("手机号格式无效，请包含国家码，例如 `+8613800000000`。")
            return
        if not self._allow_login(tenant_id):
            await event.respond("登录尝试过于频繁，请稍后再试。")
            return
        client = await self._new_client()
        try:
            await client.send_code_request(phone)
            await self._replace_attempt(
                tenant_id,
                LoginAttempt(client=client, created_at=time.monotonic(), phone=phone),
            )
            digits = phone.lstrip("+")
            masked = f"+{digits[:4]}****{digits[-4:]}" if len(digits) > 8 else phone
            await event.respond(
                f"✅ **验证码已发送到 {masked}！**\n\n"
                "请直接发送验证码（Telegram 官方发来的那串数字）：\n"
                "• 直接发送：`1 2 3 4 5`（数字之间加空格，防止被 Telegram 拦截作废）\n"
                "• 机器人收到后会立即自动删除您的验证码消息并完成绑定（**已取消 5 分钟超时限制**）。",
                buttons=Button.clear(),
            )
        except FloodWaitError as exc:
            await client.disconnect()
            await event.respond(f"Telegram 要求等待 {exc.seconds} 秒后再试。")
        except Exception as exc:
            await client.disconnect()
            logging.warning("Phone login setup failed tenant=%s: %s", tenant_id, type(exc).__name__)
            await event.respond("验证码发送失败，请检查手机号后重试。")

    async def phone(self, event) -> None:
        await self._best_effort_delete(event)
        match = re.match(r"^/phone\s+(.+)$", event.raw_text or "")
        if not match:
            await event.respond("格式: `/phone +国家码手机号`，或者点击下方按钮一键共享手机号：", buttons=[
                [Button.request_phone("📱 一键共享本机号码", resize=True, single_use=True)]
            ])
            return
        await self._start_phone_login(event, match.group(1))

    async def _verify_code_input(self, event, raw_code: str) -> None:
        tenant_id = int(event.sender_id)
        attempt = self.attempts.get(tenant_id)
        # 已移除原本的 `time.monotonic() - attempt.created_at > 300` 5分钟强制丢弃限制！
        if attempt is None or not attempt.phone:
            await event.respond("请先发送 `/phone +国家码手机号` 或点击共享号码按钮。")
            return
        code_digits = re.sub(r"\D", "", raw_code)
        if not code_digits:
            await event.respond("未能识别出数字验证码，请发送如 `1 2 3 4 5`。")
            return
        try:
            await attempt.client.sign_in(phone=attempt.phone, code=code_digits)
            await self._complete_login(tenant_id, attempt.client, event.chat_id)
        except SessionPasswordNeededError:
            attempt.waiting_password = True
            await event.respond("🔐 该账号启用了两步验证 (2FA)，请直接发送您的两步验证密码。")
        except Exception as exc:
            logging.warning("Code login failed tenant=%s: %s", tenant_id, type(exc).__name__)
            await event.respond("验证码错误或被 Telegram 拦截失效（请务必用空格隔开数字如 `1 2 3 4 5` 发送），必要时重新 `/phone`。")

    async def _verify_password_input(self, event, pwd: str) -> None:
        tenant_id = int(event.sender_id)
        attempt = self.attempts.get(tenant_id)
        # 已移除 300 秒 (5分钟) 限制
        if attempt is None or not attempt.waiting_password:
            await event.respond("当前没有等待二步验证密码的登录。")
            return
        try:
            await attempt.client.sign_in(password=pwd.strip())
            await self._complete_login(tenant_id, attempt.client, event.chat_id)
        except Exception as exc:
            logging.warning("2FA login failed tenant=%s: %s", tenant_id, type(exc).__name__)
            await event.respond("二步验证密码错误，请重试。")

    async def session_import(self, event) -> None:
        """支持直接通过 Telethon StringSession 字符串零扫码、零验证码完成绑定。"""
        await self._best_effort_delete(event)
        tenant_id = int(event.sender_id)
        match = re.match(r"^/session\s+(.+)$", event.raw_text or "", re.S)
        if not match:
            await event.respond("格式：`/session <Telethon StringSession字符串>`\n发送后消息会自动删除并验证绑定。")
            return
        session_str = match.group(1).strip()
        try:
            client = TelegramClient(
                StringSession(session_str),
                self.settings.api_id,
                self.settings.api_hash,
                proxy=_get_proxy(),
            )
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                await event.respond("❌ 该 StringSession 未授权或已失效，请检查后重试。")
                return
            await self._complete_login(tenant_id, client, event.chat_id)
        except Exception as exc:
            logging.warning("Session import failed tenant=%s: %s", tenant_id, type(exc).__name__)
            await event.respond("❌ StringSession 格式无效或连接失败。")

    async def on_contact_or_smart_input(self, event) -> None:
        """处理用户点击『共享本机号码』按钮或直接输入验证码/2FA密码（无需手打命令前缀）。"""
        text = (event.raw_text or "").strip()
        if text.startswith("/"):
            return
        # 1. 如果用户通过底部按钮分享了本人手机号 Contact
        if getattr(event.message, "contact", None):
            contact = event.message.contact
            await self._best_effort_delete(event)
            if contact.user_id and int(contact.user_id) != int(event.sender_id):
                await event.respond("⚠️ 请分享您当前使用的 Telegram 账号本机号码。")
                return
            await self._start_phone_login(event, contact.phone_number)
            return

        tenant_id = int(event.sender_id)
        attempt = self.attempts.get(tenant_id)
        if not attempt:
            return

        # 2. 如果正在等待两步验证密码，直接把非命令文本当作密码处理
        if attempt.waiting_password and text:
            await self._best_effort_delete(event)
            await self._verify_password_input(event, text)
            return

        # 3. 如果已发送验证码，且用户直接发送了包含 5 位数字的内容（或转发了官方验证码通知）
        if attempt.phone and not attempt.waiting_password:
            digits = re.findall(r"\b\d{5}\b", re.sub(r"[\s.-]", "", text))
            if digits or (4 <= len(re.sub(r"\D", "", text)) <= 6):
                await self._best_effort_delete(event)
                code_val = digits[0] if digits else re.sub(r"\D", "", text)
                await self._verify_code_input(event, code_val)

    async def _complete_login(self, tenant_id: int, client: TelegramClient, chat_id: int) -> None:
        me = await client.get_me()
        if me is None or int(me.id) != tenant_id:
            await client.log_out()
            await self._discard_attempt(tenant_id)
            await self.bot.send_message(chat_id, "绑定失败：登录账号必须与当前 Bot 用户相同。")
            return
        session = client.session.save()
        encrypted = self.cipher.encrypt(session)
        display_name = " ".join(part for part in [me.first_name, me.last_name] if part)
        self.repo.save_account(
            tenant_id,
            encrypted,
            username=me.username or "",
            display_name=display_name,
        )
        await client.disconnect()
        self.attempts.pop(tenant_id, None)
        await self.bot.send_message(
            chat_id,
            "🎉 **账号绑定成功！**\n现在可以使用 `/app` 或 `/web` 打开 Liquid Glass 控制台配置转发任务。",
            buttons=Button.clear(),
        )

    async def _discard_attempt(
        self,
        tenant_id: int,
        expected_client: Optional[TelegramClient] = None,
    ) -> None:
        attempt = self.attempts.get(tenant_id)
        if attempt and expected_client is not None and attempt.client is not expected_client:
            return
        attempt = self.attempts.pop(tenant_id, None)
        if attempt:
            await attempt.client.disconnect()

    async def app(self, event) -> None:
        url = f"{self.settings.public_base_url}/miniapp"
        await event.respond("打开转发配置：", buttons=[[KeyboardButtonWebView("🚀 打开 Mini App", url)]])

    async def web(self, event) -> None:
        tenant_id = int(event.sender_id)
        self.repo.upsert_tenant(tenant_id)
        # /web 只给登录/注册页入口，不再发免密直登链接：免密链接是谁拿到、
        # 在哪台设备打开都会直接登进该租户；要进后台请在页面上登录或注册。
        url = f"{self.settings.public_base_url}/miniapp"
        await event.respond(
            "🌐 **Web 后台入口：**\n打开后请在登录 / 注册页登录已有账号或注册新账号。",
            buttons=[[Button.url("✨ 打开 Web 后台", url)]],
        )

    async def status(self, event) -> None:
        account = self.repo.get_account(int(event.sender_id))
        if not account:
            await event.respond("尚未绑定账号，请使用 `/login`。")
            return
        tasks = self.repo.list_tasks(int(event.sender_id))
        summary: dict[str, int] = {}
        for task in tasks:
            summary[task.status.value] = summary.get(task.status.value, 0) + 1
        lines = [
            f"账号状态: {account['status']}",
            f"任务总数: {len(tasks)}",
        ]
        if account.get("last_error"):
            lines.append(f"最近错误: {account['last_error']}")
        lines.extend(f"- {key}: {value}" for key, value in sorted(summary.items()))
        await event.respond("\n".join(lines))

    async def _notify_status_changes(self) -> None:
        while True:
            notification = await asyncio.to_thread(
                self.repo.claim_status_notification,
                self.settings.worker_id,
            )
            if notification is None:
                await asyncio.sleep(2)
                continue
            tenant_id = int(notification["tenant_id"])
            event = notification["event"]
            try:
                if event == "account.error":
                    account = await asyncio.to_thread(self.repo.get_account, tenant_id)
                    if account:
                        await self.bot.send_message(
                            tenant_id,
                            f"账号需要处理: {account.get('last_error') or account['status']}\n使用 /status 查看状态。",
                        )
                else:
                    task_id = str(notification.get("metadata", {}).get("task_id", ""))
                    task = await asyncio.to_thread(self.repo.get_task, tenant_id, task_id)
                    if task:
                        if event == "task.completed":
                            text = f"任务“{task.name}”已完成。"
                        else:
                            text = f"任务“{task.name}”发生错误: {task.last_error}"
                        await self.bot.send_message(tenant_id, text)
                await asyncio.to_thread(
                    self.repo.confirm_status_notification,
                    notification["_id"],
                    self.settings.worker_id,
                )
            except Exception as exc:
                await asyncio.to_thread(
                    self.repo.release_status_notification,
                    notification["_id"],
                    self.settings.worker_id,
                    error=type(exc).__name__,
                )
                logging.info(
                    "Status notification skipped tenant=%s error=%s",
                    tenant_id,
                    type(exc).__name__,
                )

    async def logout(self, event) -> None:
        tenant_id = int(event.sender_id)
        account = self.repo.get_account(tenant_id)
        if not account:
            await event.respond("当前没有已绑定账号。")
            return
        self.repo.request_account_delete(tenant_id)
        await event.respond("任务已停止，Telegram 授权正在后台撤销并删除全部数据。")

    @staticmethod
    async def _best_effort_delete(event) -> None:
        try:
            await event.delete()
        except Exception:
            pass


async def async_main() -> None:
    settings = Settings.for_bot()
    repository = MongoRepository(settings.mongo_uri, settings.mongo_db)
    await ControlBot(settings, repository).run()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
