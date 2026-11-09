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

from .repository import DEFAULT_WEB_PASSWORD, MongoRepository
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


# Telegram 命令菜单：默认（未绑定）显示全部 9 个；已绑定只显示 5 个，不再出现登录命令。
_COMMANDS_FULL = [
    ("start", "开始使用"),
    ("login", "登录绑定方式菜单"),
    ("qr", "扫码登录"),
    ("phone", "手机号登录"),
    ("session", "导入 StringSession"),
    ("app", "打开 Mini App 控制台"),
    ("web", "打开 Web 后台入口"),
    ("zh", "查看网页登录用户名和密码"),
    ("status", "查看账号与任务状态"),
    ("logout", "解绑并删除全部数据"),
]
_COMMANDS_BOUND = [
    ("start", "开始使用"),
    ("app", "打开 Mini App 控制台"),
    ("web", "打开 Web 后台入口"),
    ("zh", "查看网页登录用户名和密码"),
    ("status", "查看账号与任务状态"),
    ("logout", "解绑并删除全部数据"),
]


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
        self.bot.add_event_handler(self.zh, events.NewMessage(pattern=r"^/zh$", func=private))
        self.bot.add_event_handler(self.status, events.NewMessage(pattern=r"^/status$", func=private))
        self.bot.add_event_handler(self.logout, events.NewMessage(pattern=r"^/logout$", func=private))
        # 处理免前缀验证码/密码输入（明文验证码与两步验证密码直接发送即可）
        self.bot.add_event_handler(self.on_contact_or_smart_input, events.NewMessage(func=private))

    async def start(self, event) -> None:
        sender = await event.get_sender()
        name = " ".join(part for part in [sender.first_name, sender.last_name] if part)
        self.repo.upsert_tenant(sender.id, username=sender.username or "", display_name=name)
        # /start 时就按 Telegram 用户名配好网页账号（用户名 + 默认密码 admin123），
        # 已有网页账号的租户不动；不用等到打开 Mini App 或绑定那一步。
        self.repo.ensure_web_login(int(sender.id), sender.username or "")
        url = f"{self.settings.public_base_url}/miniapp" if self.settings.public_base_url else ""
        account = self.repo.get_account(int(sender.id))
        bound = bool(account) and str(account.get("status", "active")) in ("active", "reauth_required")
        await self._sync_menu(int(sender.id), bound, peer=await event.get_input_sender())
        if bound:
            # 已绑定：不再展示登录步骤，给两个控制台入口。
            # 打开 Mini App 就直接进控制台：Telegram 内自动登录，浏览器里用用户名+密码登录；
            # 未绑定默认显示账号绑定页，已绑定直接进任务页。
            buttons = []
            if url:
                buttons.append([KeyboardButtonWebView("🚀 打开 Mini App 控制台", url)])
                buttons.append([Button.url("🌐 打开 Web 控制台", url)])
            await event.respond(
                "✅ **您的账号已经绑定，可以直接使用控制台配置转发任务。**\n\n"
                "`/status` 查看账号与任务状态\n"
                "`/logout` 解绑并删除全部数据",
                buttons=buttons,
            )
            return
        buttons = []
        if url:
            buttons.append([KeyboardButtonWebView("🚀 打开 Mini App 控制台", url)])
            buttons.append([Button.url("🌐 打开 Web 控制台", url)])
        # 注意：Telethon 不允许同一条消息混用内联按钮（WebView / URL）和普通键盘按钮，
        # 混用会抛 ValueError 导致整条回复发不出去，所以这里只保留内联按钮；
        # 手机号登录请走 /login 或 /phone。
        await event.respond(
            "✨ **欢迎使用 NB 转发系统旗舰版**",
            buttons=buttons,
        )

    async def _sync_menu(self, tenant_id: int, bound: bool, peer=None) -> None:
        """按绑定状态同步该私聊的命令菜单（Telegram 支持按聊天单独设置菜单）。
        已绑定只留 6 个命令，不再显示登录命令；未绑定显示全部 10 个。
        菜单同步失败不影响主流程，只记日志。"""
        try:
            from telethon.tl.functions.bots import SetBotCommandsRequest
            from telethon.tl.types import BotCommand, BotCommandScopePeer

            if peer is None:
                peer = await self.bot.get_input_entity(tenant_id)
            cmds = _COMMANDS_BOUND if bound else _COMMANDS_FULL
            await self.bot(
                SetBotCommandsRequest(
                    scope=BotCommandScopePeer(peer=peer),
                    lang_code="",
                    commands=[BotCommand(command=c, description=d) for c, d in cmds],
                )
            )
        except Exception as exc:
            logging.warning("Sync menu failed tenant=%s: %s", tenant_id, type(exc).__name__)

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
        # 注意：一键共享本机号码与 /phone 手机号登录是同一个功能，已按要求删掉前者，
        # 这里只保留手机号登录；同时 request_phone 键盘按钮也不再发送。
        msg = (
            "🔐 **请选择最适合您的登录绑定方式：**\n\n"
            "• **方式一（手机号登录）：**\n"
            "  发送 `/phone +8613800000000`（或只发 `/phone`，按提示直接输入手机号或点按钮发送），验证码会发到你的 Telegram 消息里（不是手机短信），收到后直接发送验证码\n\n"
            "• **方式二（网页可视化向导）：Web 后台登录**\n"
            "  发送 `/web` 进入网页端，在玻璃拟态面板中直接输入手机号/验证码或导入 Session\n\n"
            "• **方式三（最极速零风控）：StringSession 直接导入**\n"
            "  发送 `/session 1BVtsO...` 瞬间完成绑定\n\n"
            "• **方式四（双设备扫码 + 自动刷新）：**\n"
            "  如果您身边有另一台设备，请发送 `/qr` 获取自动刷新的二维码。"
        )
        await event.respond(msg)

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
        """自动刷新 QR token（最多持续 2 轮），避免 Telegram 服务端 30 秒令牌过期导致扫不出。"""
        try:
            qr_login = await client.qr_login()
            for round_idx in range(2):
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
                    f"📷 **二维码登录（第 {round_idx + 1}/2 次自动保鲜）**\n"
                    f"• 另一台设备：打开 Telegram → 设置 → 设备 → 扫描二维码\n"
                    f"• 注意：二维码不指定号码，哪个号扫码就绑哪个号；想绑指定号码请用 `/phone +国家码手机号`",
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
                "验证码会发到你的 Telegram 消息里（不是手机短信），请直接发送验证码（Telegram 官方发来的那串数字）：\n"
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
            await event.respond(
                "请直接输入手机号（带国家码，如 +8613800000000），或点击下方按钮发送本机号码：",
                buttons=[[Button.request_phone("📱 发送手机号", resize=True, single_use=True)]],
            )
            return
        await self._start_phone_login(event, match.group(1))

    async def _verify_code_input(self, event, raw_code: str) -> None:
        tenant_id = int(event.sender_id)
        attempt = self.attempts.get(tenant_id)
        # 已移除原本的 `time.monotonic() - attempt.created_at > 300` 5分钟强制丢弃限制！
        if attempt is None or not attempt.phone:
            await event.respond("请先发送 `/phone +国家码手机号`。")
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
        """处理手机号登录的输入：点“发送手机号”按钮分享的 Contact、直接明文输入的手机号、
        以及进行中登录的验证码/2FA密码（无需手打命令前缀）。"""
        text = (event.raw_text or "").strip()
        if text.startswith("/"):
            return
        # 1. 点“发送手机号”按钮分享了本人手机号 Contact
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
            # 没有进行中的登录时，直接输入的手机号（带国家码）也当作 /phone 处理
            normalized = re.sub(r"[\s\-]", "", text)
            if re.fullmatch(r"\+?\d{10,15}", normalized or ""):
                await self._best_effort_delete(event)
                await self._start_phone_login(event, normalized)
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
        await self._sync_menu(tenant_id, bound=True)
        await client.disconnect()
        self.attempts.pop(tenant_id, None)
        # 绑定成功：两个控制台入口（Web 在前，Mini App 在后；两个都是内联按钮，可以同框）。
        # 此时自定义键盘已在发码那一步用 Button.clear() 清掉，这里直接挂内联按钮即可。
        url = f"{self.settings.public_base_url}/miniapp" if self.settings.public_base_url else ""
        buttons = []
        if url:
            buttons.append([Button.url("🌐 打开 Web 控制台", url)])
            buttons.append([KeyboardButtonWebView("🚀 打开 Mini App 控制台", url)])
        await self.bot.send_message(
            chat_id,
            "🎉 **账号绑定成功！**\n现在可以打开 NB 控制台配置转发任务了。",
            buttons=buttons,
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

    async def zh(self, event) -> None:
        """查看当前账号的网页登录用户名和密码。
        账号是 /start 时按 Telegram 用户名自动配的（默认密码 admin123）；
        密码改过以后只存了哈希，无法取回，只能显示用户名并提示已修改。"""
        tenant_id = int(event.sender_id)
        try:
            sender = await event.get_sender()
            tg_username = (getattr(sender, "username", "") or "")
        except Exception:
            tg_username = ""
        self.repo.upsert_tenant(tenant_id, username=tg_username)
        self.repo.ensure_web_login(tenant_id, tg_username)
        info = self.repo.get_web_login(tenant_id)
        web_username = info.get("web_username") or ""
        if not web_username:
            await event.respond("暂未生成网页登录账号，请先发送 /start 再试一次。")
            return
        if info.get("must_change_password"):
            text = (
                "🔐 您的网页登录信息：\n"
                f"用户名：{web_username}\n"
                f"登录密码：{DEFAULT_WEB_PASSWORD}\n"
                "（仍是默认密码，请尽快在控制台修改）"
            )
        else:
            text = (
                "🔐 您的网页登录信息：\n"
                f"用户名：{web_username}\n"
                "登录密码：已修改过，无法查看\n"
                "忘记密码请联系管理员重置。"
            )
        await event.respond(text)

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
        await self._sync_menu(tenant_id, bound=False, peer=await event.get_input_sender())
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
