"""FastAPI control plane and Telegram Mini App host."""

from __future__ import annotations

import asyncio
import base64
import io
import os
import re
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Optional

import qrcode
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from telethon import TelegramClient, utils as telethon_utils
from nb.plugins import PluginPipeline
from telethon.crypto import AuthKey
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.sessions import StringSession

from .models import (
    AccountStatus,
    AccountView,
    AdminPasswordLoginRequest,
    AdminSettingsUpdateRequest,
    AdminTenantUpdateRequest,
    DialogItem,
    PluginSandboxRequest,
    PluginSandboxResponse,
    UserLoginRequest,
    UserRegisterRequest,
    ChangePasswordRequest,
    ForwardTask,
    ForwardTaskInput,
    MiniAppAuthRequest,
    PhoneSendCodeRequest,
    PhoneVerifyCodeRequest,
    SessionImportRequest,
    SessionResponse,
    TaskUpdate,
    TenantView,
)
from .repository import MongoRepository, RevisionConflict
from .security import SessionCipher, expiry, get_telegram_proxy, validate_miniapp_init_data, verify_password
from .settings import Settings

COOKIE_NAME = "nb_session"
STATIC_DIR = Path(__file__).with_name("static")
MAX_WATERMARK_BYTES = 5 * 1024 * 1024
MAX_WATERMARK_DIMENSION = 4096
MAX_WATERMARK_PIXELS = 16_000_000


def _sqlite_session_to_string(data: bytes) -> str:
    """Extract dc_id, server_address, port, and auth_key from a .session SQLite file and build a Telethon StringSession."""
    with tempfile.NamedTemporaryFile(suffix=".session", delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        conn = sqlite3.connect(tmp_path)
        cur = conn.cursor()
        row = cur.execute(
            "SELECT dc_id, server_address, port, auth_key FROM sessions LIMIT 1"
        ).fetchone()
        conn.close()
        if not row or not row[3]:
            raise ValueError("Invalid .session file")
        dc_id, server_address, port, auth_key_bytes = row
        ss = StringSession()
        ss.set_dc(int(dc_id), str(server_address), int(port))
        ss.auth_key = AuthKey(data=bytes(auth_key_bytes))
        return ss.save()
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def create_app(
    settings: Optional[Settings] = None,
    repository: Optional[MongoRepository] = None,
) -> FastAPI:
    cfg = settings or Settings.for_api()
    repo = repository or MongoRepository(cfg.mongo_uri, cfg.mongo_db)
    cipher = SessionCipher(cfg.session_encryption_key)
    pending_web_logins: dict[int, tuple[TelegramClient, str]] = {}
    pending_qr_logins: dict[int, tuple[TelegramClient, object]] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        repo.ensure_indexes()
        repo.ping()
        yield
        for client, _ in list(pending_web_logins.values()):
            try:
                await client.disconnect()
            except Exception:
                pass
        for client, _ in list(pending_qr_logins.values()):
            try:
                await client.disconnect()
            except Exception:
                pass
        pending_web_logins.clear()
        pending_qr_logins.clear()

    app = FastAPI(
        title="nb multi-tenant control plane",
        version="3.3.0",
        lifespan=lifespan,
    )
    app.state.settings = cfg
    app.state.repository = repo
    app.mount("/miniapp/static", StaticFiles(directory=STATIC_DIR), name="miniapp-static")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="root-static")

    def current_tenant(
        request: Request,
        authorization: Annotated[Optional[str], Header()] = None,
    ) -> int:
        token = ""
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization.split(" ", 1)[1].strip()
        if not token:
            token = request.cookies.get(COOKIE_NAME, "")
        request.state.session_token = token
        tenant_id = repo.resolve_session(token)
        if tenant_id is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        return tenant_id

    def _is_admin_tenant(tenant_id: int) -> bool:
        if tenant_id in cfg.admin_ids:
            return True
        doc = repo.tenants.find_one({"_id": tenant_id}, {"is_admin": 1})
        return bool(doc and doc.get("is_admin"))

    def require_admin(tenant_id: int = Depends(current_tenant)) -> int:
        if not _is_admin_tenant(tenant_id):
            raise HTTPException(status_code=403, detail="Administrator required")
        return tenant_id

    @app.get("/health")
    @app.get("/healthz")
    def health() -> dict[str, str]:
        repo.ping()
        return {"status": "ok"}

    @app.get("/miniapp", include_in_schema=False)
    @app.get("/miniapp/", include_in_schema=False)
    def miniapp() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.post("/api/auth/miniapp", response_model=SessionResponse)
    def miniapp_auth(
        payload: MiniAppAuthRequest,
        request: Request,
        response: Response,
    ) -> SessionResponse:
        try:
            user = validate_miniapp_init_data(payload.init_data, cfg.bot_token)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        client_ip = request.client.host if request.client else "unknown"
        if not repo.consume_rate_limit(
            "miniapp-tenant",
            str(user.id),
            limit=20,
            window_seconds=60,
            tenant_id=user.id,
        ) or not repo.consume_rate_limit(
            "miniapp-ip",
            client_ip,
            limit=120,
            window_seconds=60,
        ):
            raise HTTPException(status_code=429, detail="Too many login attempts")
        display_name = " ".join(part for part in [user.first_name, user.last_name] if part)
        repo.upsert_tenant(user.id, username=user.username, display_name=display_name)
        session_exp = expiry(hours=cfg.web_session_hours) if cfg.web_session_hours > 0 else None
        token, expires_at = repo.create_session(
            user.id, kind="miniapp", expires_at=session_exp
        )
        is_https = cfg.public_base_url.startswith("https://")
        cookie_max_age = cfg.web_session_hours * 3600 if cfg.web_session_hours > 0 else 315360000
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=cookie_max_age,
            httponly=True,
            secure=is_https,
            samesite="lax",
            path="/",
        )
        return SessionResponse(token=token, expires_at=expires_at)

    @app.post("/api/auth/password", response_model=SessionResponse)
    def admin_password_login(
        payload: AdminPasswordLoginRequest,
        response: Response,
    ) -> SessionResponse:
        expected = cfg.admin_password or os.getenv("PASSWORD", "admin")
        if not payload.password or payload.password != expected:
            raise HTTPException(status_code=401, detail="管理员密码错误")
        target_tenant = payload.tenant_id or (next(iter(cfg.admin_ids)) if cfg.admin_ids else 1)
        repo.upsert_tenant(int(target_tenant), username="admin", display_name="Admin")
        session_exp = expiry(hours=cfg.web_session_hours) if cfg.web_session_hours > 0 else None
        token, expires_at = repo.create_session(
            int(target_tenant), kind="browser", expires_at=session_exp
        )
        is_https = cfg.public_base_url.startswith("https://")
        cookie_max_age = cfg.web_session_hours * 3600 if cfg.web_session_hours > 0 else 315360000
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=cookie_max_age,
            httponly=True,
            secure=is_https,
            samesite="lax",
            path="/",
        )
        return SessionResponse(token=token, expires_at=expires_at)

    @app.post("/api/auth/register", response_model=SessionResponse, status_code=201)
    def register_user(
        payload: UserRegisterRequest,
        response: Response,
    ) -> SessionResponse:
        try:
            tid = repo.register_web_tenant(
                payload.username,
                payload.password,
                display_name=payload.display_name,
                tenant_id=payload.tenant_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        session_exp = expiry(hours=cfg.web_session_hours) if cfg.web_session_hours > 0 else None
        token, expires_at = repo.create_session(tid, kind="browser", expires_at=session_exp)
        is_https = cfg.public_base_url.startswith("https://")
        cookie_max_age = cfg.web_session_hours * 3600 if cfg.web_session_hours > 0 else 315360000
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=cookie_max_age,
            httponly=True,
            secure=is_https,
            samesite="lax",
            path="/",
        )
        return SessionResponse(token=token, expires_at=expires_at)

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    def admin_portal() -> FileResponse:
        return FileResponse(STATIC_DIR / "admin.html")

    @app.post("/api/admin/auth/login", response_model=SessionResponse)
    def admin_portal_login(
        payload: UserLoginRequest,
        response: Response,
    ) -> SessionResponse:
        adm_cfg = repo.get_admin_settings(
            default_username=cfg.admin_username,
            default_password=cfg.admin_password or os.getenv("PASSWORD", "admin"),
            default_path=cfg.admin_path,
        )
        u = payload.username.strip()
        if u.lower() == adm_cfg["admin_username"].lower() and verify_password(payload.password, adm_cfg["admin_password_hash"]):
            tid = next(iter(cfg.admin_ids)) if cfg.admin_ids else 1
            repo.upsert_tenant(int(tid), username=adm_cfg["admin_username"], display_name="管理员")
            repo.tenants.update_one({"_id": int(tid)}, {"$set": {"is_admin": True}})
        else:
            tid = repo.authenticate_web_tenant(u, payload.password)
            if tid is None or not _is_admin_tenant(int(tid)):
                raise HTTPException(status_code=401, detail="管理员账号或密码错误")
        session_exp = expiry(hours=cfg.web_session_hours) if cfg.web_session_hours > 0 else None
        token, expires_at = repo.create_session(int(tid), kind="browser", expires_at=session_exp)
        is_https = cfg.public_base_url.startswith("https://")
        cookie_max_age = cfg.web_session_hours * 3600 if cfg.web_session_hours > 0 else 315360000
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=cookie_max_age,
            httponly=True,
            secure=is_https,
            samesite="lax",
            path="/",
        )
        return SessionResponse(token=token, expires_at=expires_at)

    @app.get("/api/admin/settings")
    def get_admin_config(_: int = Depends(require_admin)) -> dict:
        adm_cfg = repo.get_admin_settings(
            default_username=cfg.admin_username,
            default_password=cfg.admin_password or os.getenv("PASSWORD", "admin"),
            default_path=cfg.admin_path,
        )
        return {
            "admin_username": adm_cfg["admin_username"],
            "admin_path": adm_cfg["admin_path"],
        }

    @app.put("/api/admin/settings")
    def update_admin_config(
        payload: AdminSettingsUpdateRequest,
        _: int = Depends(require_admin),
    ) -> dict:
        return repo.update_admin_settings(
            admin_username=payload.admin_username,
            admin_password=payload.admin_password,
            admin_path=payload.admin_path,
        )

    @app.post("/api/auth/login", response_model=SessionResponse)
    def login_user(
        payload: UserLoginRequest,
        response: Response,
    ) -> SessionResponse:
        tid = repo.authenticate_web_tenant(payload.username, payload.password)
        if tid is None:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        session_exp = expiry(hours=cfg.web_session_hours) if cfg.web_session_hours > 0 else None
        token, expires_at = repo.create_session(int(tid), kind="browser", expires_at=session_exp)
        is_https = cfg.public_base_url.startswith("https://")
        cookie_max_age = cfg.web_session_hours * 3600 if cfg.web_session_hours > 0 else 315360000
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=cookie_max_age,
            httponly=True,
            secure=is_https,
            samesite="lax",
            path="/",
        )
        return SessionResponse(token=token, expires_at=expires_at)

    @app.post("/api/auth/change-password")
    def change_password(
        payload: ChangePasswordRequest,
        tenant_id: int = Depends(current_tenant),
    ) -> dict:
        if not repo.change_web_password(tenant_id, payload.old_password, payload.new_password):
            raise HTTPException(status_code=401, detail="原密码错误或账号未设置网页密码")
        return {"ok": True}

    @app.get("/auth/magic/{token}", include_in_schema=False)
    def magic_login(token: str) -> RedirectResponse:
        result = repo.consume_magic_link(
            token,
            session_hours=cfg.web_session_hours,
            reusable=cfg.magic_link_Reusable,
        )
        if result is None:
            raise HTTPException(status_code=401, detail="Link is invalid or revoked")
        session_token, _ = result
        is_https = cfg.public_base_url.startswith("https://")
        target_url = f"{cfg.public_base_url}/miniapp" if cfg.public_base_url else "/miniapp"
        response = RedirectResponse(url=target_url, status_code=303)
        cookie_max_age = cfg.web_session_hours * 3600 if cfg.web_session_hours > 0 else 315360000
        response.set_cookie(
            COOKIE_NAME,
            session_token,
            max_age=cookie_max_age,
            httponly=True,
            secure=is_https,
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/api/logout", status_code=204)
    def browser_logout(
        request: Request,
        _: int = Depends(current_tenant),
    ) -> Response:
        repo.revoke_session(getattr(request.state, "session_token", ""))
        response = Response(status_code=204)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    def _build_tenant_view(tenant_id: int) -> TenantView:
        account_doc = repo.get_account(tenant_id)
        account_view = AccountView.model_validate(account_doc) if account_doc else None
        tenant_doc = repo.tenants.find_one({"_id": tenant_id}) or {}
        return TenantView(
            tenant_id=tenant_id,
            is_admin=_is_admin_tenant(tenant_id),
            account_status=account_doc.get("status") if account_doc else None,
            account=account_view,
            web_username=str(tenant_doc.get("web_username") or ""),
            must_change_password=bool(tenant_doc.get("must_change_password") or False),
        )

    @app.get("/api/me", response_model=TenantView)
    @app.get("/api/tenant/me", response_model=TenantView)
    def me(tenant_id: int = Depends(current_tenant)) -> TenantView:
        return _build_tenant_view(tenant_id)

    @app.get("/api/account", response_model=AccountView)
    def account(tenant_id: int = Depends(current_tenant)) -> AccountView:
        doc = repo.get_account(tenant_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="No Telegram account is bound")
        return AccountView.model_validate(doc)

    # ==================== 登录方式 1：手机号 + 验证码 + 语音电话呼叫兜底 ====================
    @app.post("/api/account/phone/send")
    async def web_phone_send_code(
        payload: PhoneSendCodeRequest,
        tenant_id: int = Depends(current_tenant),
    ) -> dict[str, str]:
        if not cfg.api_id or not cfg.api_hash:
            raise HTTPException(status_code=503, detail="API_ID / API_HASH not configured")
        phone = re.sub(r"[\s()-]", "", payload.phone)
        if not phone.startswith("+"):
            phone = "+" + phone
        if not re.fullmatch(r"\+\d{7,15}", phone):
            raise HTTPException(status_code=422, detail="手机号需包含国家码，如 +8613800000000")
        old = pending_web_logins.pop(tenant_id, None)
        if old:
            await old[0].disconnect()
        client = TelegramClient(
            StringSession(),
            cfg.api_id,
            cfg.api_hash,
            proxy=get_telegram_proxy(),
        )
        await client.connect()
        try:
            sent_code = await client.send_code_request(phone)
            repo.save_phone_login_challenge(
                tenant_id,
                phone=phone,
                phone_code_hash=getattr(sent_code, "phone_code_hash", ""),
                encrypted_temp_session=cipher.encrypt(client.session.save()),
            )
            pending_web_logins[tenant_id] = (client, phone)
            return {"status": "code_sent", "phone": phone}
        except FloodWaitError as exc:
            await client.disconnect()
            raise HTTPException(status_code=429, detail=f"请等待 {exc.seconds} 秒后再试") from exc
        except Exception as exc:
            await client.disconnect()
            raise HTTPException(status_code=400, detail=f"发送失败: {type(exc).__name__}") from exc

    @app.post("/api/account/phone/verify", response_model=AccountView)
    async def web_phone_verify_code(
        payload: PhoneVerifyCodeRequest,
        tenant_id: int = Depends(current_tenant),
    ) -> AccountView:
        entry = pending_web_logins.get(tenant_id)
        phone_code_hash = None
        if entry:
            client, phone = entry
        else:
            saved = repo.get_phone_login_challenge(tenant_id)
            if not saved:
                raise HTTPException(status_code=400, detail="请先获取验证码")
            phone = saved["phone"]
            phone_code_hash = saved.get("phone_code_hash")
            temp_sess = cipher.decrypt(saved["encrypted_temp_session"])
            client = TelegramClient(
                StringSession(temp_sess),
                cfg.api_id,
                cfg.api_hash,
                proxy=get_telegram_proxy(),
            )
            await client.connect()
        code_digits = re.sub(r"\D", "", payload.code)
        try:
            if code_digits:
                try:
                    await client.sign_in(phone=phone, code=code_digits, phone_code_hash=phone_code_hash)
                except SessionPasswordNeededError:
                    if not payload.password:
                        raise HTTPException(status_code=428, detail="2FA_REQUIRED")
                    await client.sign_in(password=payload.password)
            elif payload.password:
                await client.sign_in(password=payload.password)
            else:
                raise HTTPException(status_code=422, detail="请输入验证码或密码")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"验证失败: {type(exc).__name__}") from exc

        me = await client.get_me()
        if me is None or int(me.id) != tenant_id:
            await client.log_out()
            await client.disconnect()
            pending_web_logins.pop(tenant_id, None)
            raise HTTPException(status_code=403, detail="登录账号必须与当前租户一致")
        session_str = client.session.save()
        encrypted = cipher.encrypt(session_str)
        display_name = " ".join(part for part in [me.first_name, me.last_name] if part)
        repo.save_account(tenant_id, encrypted, username=me.username or "", display_name=display_name)
        await client.disconnect()
        pending_web_logins.pop(tenant_id, None)
        repo.clear_phone_login_challenge(tenant_id)
        return AccountView.model_validate(repo.get_account(tenant_id))

    # ==================== 登录方式 2：StringSession 字符串导入 ====================
    @app.post("/api/account/session", response_model=AccountView)
    async def web_import_session(
        payload: SessionImportRequest,
        tenant_id: int = Depends(current_tenant),
    ) -> AccountView:
        if not cfg.api_id or not cfg.api_hash:
            raise HTTPException(status_code=503, detail="API_ID / API_HASH not configured")
        client = TelegramClient(
            StringSession(payload.session_string.strip()),
            cfg.api_id,
            cfg.api_hash,
            proxy=get_telegram_proxy(),
        )
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise HTTPException(status_code=400, detail="StringSession 无效或未授权")
            me = await client.get_me()
            if me is None or int(me.id) != tenant_id:
                raise HTTPException(status_code=403, detail="Session 对应的账号必须与当前租户一致")
            encrypted = cipher.encrypt(payload.session_string.strip())
            display_name = " ".join(part for part in [me.first_name, me.last_name] if part)
            repo.save_account(tenant_id, encrypted, username=me.username or "", display_name=display_name)
        finally:
            await client.disconnect()
        return AccountView.model_validate(repo.get_account(tenant_id))

    # ==================== 登录方式 3：上传 .session 文件导入 ====================
    @app.post("/api/account/session-file", response_model=AccountView)
    async def web_import_session_file(
        file: UploadFile = File(),
        tenant_id: int = Depends(current_tenant),
    ) -> AccountView:
        """直接上传 Telethon .session SQLite 文件转换并绑定账号。"""
        if not cfg.api_id or not cfg.api_hash:
            raise HTTPException(status_code=503, detail="API_ID / API_HASH not configured")
        raw = await file.read(2 * 1024 * 1024)
        try:
            session_str = _sqlite_session_to_string(raw)
        except Exception as exc:
            raise HTTPException(status_code=422, detail="无效的 .session 文件") from exc
        return await web_import_session(SessionImportRequest(session_string=session_str), tenant_id)

    # ==================== 登录方式 4：tg://login 一键唤起客户端授权 / 动态二维码 ====================
    @app.post("/api/account/qr/start")
    async def web_qr_start(
        tenant_id: int = Depends(current_tenant),
    ) -> dict[str, str]:
        """生成 tg://login?token=... 授权链接与 Base64 二维码图片。"""
        if not cfg.api_id or not cfg.api_hash:
            raise HTTPException(status_code=503, detail="API_ID / API_HASH not configured")
        old = pending_qr_logins.pop(tenant_id, None)
        if old:
            await old[0].disconnect()
        client = TelegramClient(
            StringSession(),
            cfg.api_id,
            cfg.api_hash,
            proxy=get_telegram_proxy(),
        )
        await client.connect()
        qr_login = await client.qr_login()
        pending_qr_logins[tenant_id] = (client, qr_login)
        img = qrcode.make(qr_login.url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64_img = base64.b64encode(buf.getvalue()).decode("ascii")
        return {
            "url": qr_login.url,
            "qr_image": f"data:image/png;base64,{b64_img}",
        }

    @app.get("/api/account/qr/poll")
    async def web_qr_poll(
        tenant_id: int = Depends(current_tenant),
    ) -> dict[str, str]:
        """检查 tg://login 唤起或扫码是否已在客户端确认。"""
        entry = pending_qr_logins.get(tenant_id)
        if not entry:
            return {"status": "idle"}
        client, qr_login = entry
        try:
            await asyncio.wait_for(qr_login.wait(), timeout=2.0)
            me = await client.get_me()
            if me is None or int(me.id) != tenant_id:
                await client.log_out()
                await client.disconnect()
                pending_qr_logins.pop(tenant_id, None)
                raise HTTPException(status_code=403, detail="登录账号与当前租户不一致")
            encrypted = cipher.encrypt(client.session.save())
            display_name = " ".join(part for part in [me.first_name, me.last_name] if part)
            repo.save_account(tenant_id, encrypted, username=me.username or "", display_name=display_name)
            await client.disconnect()
            pending_qr_logins.pop(tenant_id, None)
            return {"status": "bound", "username": me.username or str(tenant_id)}
        except asyncio.TimeoutError:
            return {"status": "waiting"}
        except SessionPasswordNeededError:
            return {"status": "2fa_required"}

    # ==================== 深度优化 1：已加入频道与群组自动下拉选择器 ====================
    @app.get("/api/account/dialogs", response_model=list[DialogItem])
    async def list_account_dialogs(
        tenant_id: int = Depends(current_tenant),
    ) -> list[DialogItem]:
        doc = repo.get_account(tenant_id)
        if not doc or not doc.get("encrypted_session"):
            raise HTTPException(status_code=404, detail="请先绑定 Telegram 账号")
        session_str = cipher.decrypt(doc["encrypted_session"])
        client = TelegramClient(
            StringSession(session_str),
            cfg.api_id,
            cfg.api_hash,
            proxy=get_telegram_proxy(),
        )
        await client.connect()
        items: list[DialogItem] = []
        try:
            async for dialog in client.iter_dialogs(limit=150):
                entity = dialog.entity
                peer_id = telethon_utils.get_peer_id(entity)
                title = getattr(dialog, "name", "") or getattr(entity, "title", "") or str(peer_id)
                username = getattr(entity, "username", "") or ""
                if getattr(dialog, "is_channel", False) and getattr(entity, "broadcast", False):
                    kind = "channel"
                elif getattr(dialog, "is_group", False) or getattr(dialog, "is_channel", False):
                    kind = "group"
                elif getattr(entity, "bot", False):
                    kind = "bot"
                else:
                    kind = "user"
                items.append(
                    DialogItem(
                        peer_id=int(peer_id),
                        title=title,
                        username=username,
                        kind=kind,
                        can_post=True,
                    )
                )
        finally:
            await client.disconnect()
        return items

    # ==================== 深度优化 2：插件规则实时沙盒测试器 ====================
    @app.post("/api/plugins/preview", response_model=PluginSandboxResponse)
    def preview_plugin_pipeline(
        payload: PluginSandboxRequest,
    ) -> PluginSandboxResponse:
        pipeline = PluginPipeline(payload.plugins)
        res = pipeline.dry_run_text(
            payload.sample_text,
            file_type=payload.file_type,
            sender=payload.sender,
        )
        return PluginSandboxResponse(
            passed_filter=res.passed_filter,
            filtered_reason=res.filtered_reason,
            original_text=res.original_text,
            transformed_text=res.transformed_text,
            applied_plugins=res.applied_plugins,
        )

    # ==================== 深度优化 3：实时转发投递流水与进度流 ====================
    @app.get("/api/deliveries/recent")
    def recent_deliveries(
        tenant_id: int = Depends(current_tenant),
    ) -> list[dict]:
        return repo.list_recent_deliveries(tenant_id=tenant_id, limit=30)

    @app.get("/api/admin/deliveries/recent")
    def admin_recent_deliveries(
        _: int = Depends(require_admin),
    ) -> list[dict]:
        return repo.list_recent_deliveries(tenant_id=None, limit=50)

    @app.delete("/api/account", status_code=202)
    def delete_account(tenant_id: int = Depends(current_tenant)) -> dict[str, str]:
        if not repo.request_account_delete(tenant_id):
            raise HTTPException(status_code=404, detail="No Telegram account is bound")
        return {"status": "deleting"}

    @app.get("/api/tasks", response_model=list[ForwardTask])
    def list_tasks(tenant_id: int = Depends(current_tenant)) -> list[ForwardTask]:
        return repo.list_tasks(tenant_id)

    @app.post("/api/tasks", response_model=ForwardTask, status_code=201)
    def create_task(
        payload: ForwardTaskInput,
        tenant_id: int = Depends(current_tenant),
    ) -> ForwardTask:
        account_doc = repo.get_account(tenant_id)
        if account_doc is None or account_doc.get("status") != AccountStatus.ACTIVE.value:
            raise HTTPException(status_code=409, detail="请先绑定并激活 Telegram 账号")
        try:
            return repo.create_task(tenant_id, payload)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.get("/api/tasks/{task_id}", response_model=ForwardTask)
    def get_task(
        task_id: str,
        tenant_id: int = Depends(current_tenant),
    ) -> ForwardTask:
        task = repo.get_task(tenant_id, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return task

    @app.put("/api/tasks/{task_id}", response_model=ForwardTask)
    def update_task(
        task_id: str,
        payload: TaskUpdate,
        tenant_id: int = Depends(current_tenant),
    ) -> ForwardTask:
        try:
            return repo.update_task(tenant_id, task_id, payload.revision, payload.task)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Task not found") from exc
        except RevisionConflict as exc:
            raise HTTPException(
                status_code=409, detail="Task was changed in another session"
            ) from exc

    @app.delete("/api/tasks/{task_id}", status_code=204)
    def delete_task(
        task_id: str,
        tenant_id: int = Depends(current_tenant),
    ) -> Response:
        if not repo.delete_task(tenant_id, task_id):
            raise HTTPException(status_code=404, detail="Task not found")
        return Response(status_code=204)

    def _task_action(
        task_id: str,
        action: str,
        tenant_id: int,
    ) -> ForwardTask:
        normalized_action = "run" if action == "start" else action
        task = repo.get_task(tenant_id, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        account_doc = repo.get_account(tenant_id)
        if normalized_action != "pause" and (
            account_doc is None or account_doc.get("status") != AccountStatus.ACTIVE.value
        ):
            raise HTTPException(status_code=409, detail="Telegram account is not active")
        if normalized_action != "pause" and (not task.sources or not task.destinations):
            raise HTTPException(
                status_code=422, detail="At least one source and destination are required"
            )
        return repo.set_task_action(tenant_id, task_id, normalized_action)

    @app.post("/api/tasks/{task_id}/validate", response_model=ForwardTask)
    def validate_task(
        task_id: str,
        tenant_id: int = Depends(current_tenant),
    ) -> ForwardTask:
        return _task_action(task_id, "validate", tenant_id)

    @app.post("/api/tasks/{task_id}/run", response_model=ForwardTask)
    @app.post("/api/tasks/{task_id}/start", response_model=ForwardTask)
    def run_task(
        task_id: str,
        tenant_id: int = Depends(current_tenant),
    ) -> ForwardTask:
        return _task_action(task_id, "run", tenant_id)

    @app.post("/api/tasks/{task_id}/pause", response_model=ForwardTask)
    def pause_task(
        task_id: str,
        tenant_id: int = Depends(current_tenant),
    ) -> ForwardTask:
        return _task_action(task_id, "pause", tenant_id)

    @app.get("/api/tasks/{task_id}/status", response_model=ForwardTask)
    def task_status(
        task_id: str,
        tenant_id: int = Depends(current_tenant),
    ) -> ForwardTask:
        task = repo.get_task(tenant_id, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return task

    @app.post("/api/tasks/{task_id}/watermark")
    async def upload_watermark(
        task_id: str,
        file: UploadFile = File(),
        tenant_id: int = Depends(current_tenant),
    ) -> dict[str, str]:
        if repo.get_task(tenant_id, task_id) is None:
            raise HTTPException(status_code=404, detail="Task not found")
        data = await file.read(MAX_WATERMARK_BYTES + 1)
        if len(data) > MAX_WATERMARK_BYTES or not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise HTTPException(
                status_code=422, detail="Watermark must be a PNG up to 5 MB"
            )
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "PNG":
                    raise UnidentifiedImageError("not PNG")
                width, height = image.size
                if (
                    width > MAX_WATERMARK_DIMENSION
                    or height > MAX_WATERMARK_DIMENSION
                    or width * height > MAX_WATERMARK_PIXELS
                ):
                    raise UnidentifiedImageError("PNG dimensions are too large")
                image.verify()
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
            raise HTTPException(status_code=422, detail="Watermark PNG is invalid") from exc
        try:
            asset_id = repo.put_watermark(tenant_id, task_id, data)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Task not found") from exc
        return {"asset_id": asset_id}

    @app.delete("/api/tasks/{task_id}/watermark", status_code=204)
    def delete_watermark(
        task_id: str,
        tenant_id: int = Depends(current_tenant),
    ) -> Response:
        task = repo.get_task(tenant_id, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if hasattr(repo, "delete_watermark"):
            repo.delete_watermark(tenant_id, task_id)
        else:
            repo.tasks.update_one(
                {"_id": task_id, "tenant_id": tenant_id},
                {"$set": {"watermark_asset_id": None}, "$inc": {"revision": 1}},
            )
        return Response(status_code=204)

    # ==================== 管理员跨租户全量配置与任务管理 API ====================
    @app.get("/api/admin/tenants")
    def admin_tenants(_: int = Depends(require_admin)) -> list[dict]:
        return repo.list_all_tenants_detailed()

    @app.post("/api/admin/tenants", status_code=201)
    def admin_create_tenant(
        payload: UserRegisterRequest,
        _: int = Depends(require_admin),
    ) -> dict:
        try:
            tid = repo.register_web_tenant(
                payload.username,
                payload.password,
                display_name=payload.display_name,
                tenant_id=payload.tenant_id,
            )
            return {"tenant_id": tid, "username": payload.username, "status": "active"}
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.put("/api/admin/tenants/{target_tenant_id}")
    def admin_update_tenant(
        target_tenant_id: int,
        payload: AdminTenantUpdateRequest,
        _: int = Depends(require_admin),
    ) -> dict:
        return repo.update_tenant_by_admin(
            target_tenant_id,
            username=payload.username,
            display_name=payload.display_name,
            status=payload.status,
            is_admin=payload.is_admin,
            new_password=payload.new_password,
            max_tasks=payload.max_tasks,
            allow_heavy_plugins=payload.allow_heavy_plugins,
            expire_at=payload.expire_at,
        )

    @app.delete("/api/admin/tenants/{target_tenant_id}", status_code=204)
    def admin_delete_tenant(
        target_tenant_id: int,
        _: int = Depends(require_admin),
    ) -> Response:
        repo.hard_delete_account(target_tenant_id)
        return Response(status_code=204)

    @app.get("/api/admin/tenants/{target_tenant_id}/tasks", response_model=list[ForwardTask])
    def admin_list_tenant_tasks(
        target_tenant_id: int,
        _: int = Depends(require_admin),
    ) -> list[ForwardTask]:
        return repo.list_tasks(target_tenant_id)

    @app.post("/api/admin/tenants/{target_tenant_id}/tasks", response_model=ForwardTask, status_code=201)
    def admin_create_tenant_task(
        target_tenant_id: int,
        payload: ForwardTaskInput,
        _: int = Depends(require_admin),
    ) -> ForwardTask:
        repo.upsert_tenant(target_tenant_id)
        return repo.create_task(target_tenant_id, payload)

    @app.put("/api/admin/tenants/{target_tenant_id}/tasks/{task_id}", response_model=ForwardTask)
    def admin_update_tenant_task(
        target_tenant_id: int,
        task_id: str,
        payload: TaskUpdate,
        _: int = Depends(require_admin),
    ) -> ForwardTask:
        try:
            return repo.update_task(target_tenant_id, task_id, payload.revision, payload.task)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Task not found") from exc
        except RevisionConflict as exc:
            raise HTTPException(status_code=409, detail="Task revision conflict") from exc

    @app.post("/api/admin/tenants/{target_tenant_id}/tasks/{task_id}/{action}", response_model=ForwardTask)
    def admin_action_tenant_task(
        target_tenant_id: int,
        task_id: str,
        action: str,
        _: int = Depends(require_admin),
    ) -> ForwardTask:
        norm = "run" if action == "start" else action
        if norm not in {"validate", "run", "pause"}:
            raise HTTPException(status_code=422, detail="Invalid action")
        try:
            return repo.set_task_action(target_tenant_id, task_id, norm)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Task not found") from exc

    @app.delete("/api/admin/tenants/{target_tenant_id}/tasks/{task_id}", status_code=204)
    def admin_delete_tenant_task(
        target_tenant_id: int,
        task_id: str,
        _: int = Depends(require_admin),
    ) -> Response:
        if not repo.delete_task(target_tenant_id, task_id):
            raise HTTPException(status_code=404, detail="Task not found")
        return Response(status_code=204)

    @app.get("/api/admin/workers")
    def admin_workers(_: int = Depends(require_admin)) -> list[dict]:
        return list(repo.leases.find({}, {"_id": 0}))

    @app.get("/api/admin/errors")
    def admin_errors(_: int = Depends(require_admin)) -> list[dict]:
        return repo.list_system_errors()

    @app.get("/{custom_admin_slug}", include_in_schema=False)
    def custom_admin_portal(custom_admin_slug: str) -> FileResponse:
        adm_cfg = repo.get_admin_settings(
            default_username=cfg.admin_username,
            default_password=cfg.admin_password or os.getenv("PASSWORD", "admin"),
            default_path=cfg.admin_path,
        )
        expected_slug = adm_cfg["admin_path"].lstrip("/")
        if custom_admin_slug == expected_slug:
            return FileResponse(STATIC_DIR / "admin.html")
        raise HTTPException(status_code=404, detail="Not Found")

    return app


def main() -> None:
    import os
    import uvicorn

    uvicorn.run(
        "nb.multitenant.api:create_app",
        factory=True,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
