"""Load all user defined config and env vars."""

import logging
import os
import sys
from typing import Any, Dict, List, Optional, Union

from dotenv import load_dotenv
from pydantic import BaseModel, validator
from pymongo import MongoClient
from telethon import TelegramClient
from telethon.sessions import StringSession

from nb import storage as stg
from nb.const import CONFIG_FILE_NAME
from nb.plugin_models import PluginConfig

pwd = os.getcwd()
env_file = os.path.join(pwd, ".env")

load_dotenv(env_file)


class CommentsConfig(BaseModel):
    """评论区转发配置"""

    enabled: bool = False

    source_mode: str = "comments"
    source_discussion_group: Optional[Union[int, str]] = None

    dest_mode: str = "comments"
    dest_discussion_groups: List[Union[int, str]] = []

    only_media: bool = False
    include_text_comments: bool = True
    skip_bot_comments: bool = False
    skip_admin_comments: bool = False

    post_mapping_mode: str = "auto"
    manual_post_mapping: Dict[str, str] = {}
    manual_post_mapping_raw: str = ""


class Forward(BaseModel):
    """Blueprint for the forward object."""

    con_name: str = ""
    use_this: bool = True
    source: Union[int, str] = ""
    dest: List[Union[int, str]] = []
    offset: int = 0
    end: Optional[int] = None
    comments: CommentsConfig = CommentsConfig()


class LiveSettings(BaseModel):
    """Settings to configure how nb operates in live mode."""

    sequential_updates: bool = False
    delete_sync: bool = False
    delete_on_edit: Optional[str] = ".deleteMe"


class PastSettings(BaseModel):
    """Configuration for past mode."""

    delay: int = 0

    @validator("delay")
    def validate_delay(cls, val):
        if val not in range(0, 101):
            logging.warning("delay must be within 0 to 100 seconds")
            if val > 100:
                val = 100
            if val < 0:
                val = 0
        return val


class LoginConfig(BaseModel):

    API_ID: int = 0
    API_HASH: str = ""
    user_type: int = 0  # 0:bot, 1:user
    phone_no: int = 91
    USERNAME: str = ""
    SESSION_STRING: str = ""
    BOT_TOKEN: str = ""


class BotMessages(BaseModel):
    start: str = "Hi! I am alive"
    bot_help: str = "For details visit github.com/artai8/nb"


class Config(BaseModel):
    """The blueprint for nb's whole config."""

    pid: int = 0
    theme: str = "light"
    login: LoginConfig = LoginConfig()
    admins: List[Union[int, str]] = []
    forwards: List[Forward] = []
    show_forwarded_from: bool = False
    mode: int = 0  # 0: live, 1:past
    live: LiveSettings = LiveSettings()
    past: PastSettings = PastSettings()

    plugins: PluginConfig = PluginConfig()
    bot_messages: BotMessages = BotMessages()


def write_config_to_file(config: Config):
    with open(CONFIG_FILE_NAME, "w", encoding="utf8") as file:
        file.write(config.json())


def detect_config_type() -> int:
    if MONGO_CON_STR:
        logging.info("Using mongo db for storing config!")
        client = MongoClient(MONGO_CON_STR)
        stg.mycol = setup_mongo(client)
        return 2
    if CONFIG_FILE_NAME in os.listdir():
        logging.info(f"{CONFIG_FILE_NAME} detected!")
        return 1
    else:
        logging.info(
            "config file not found. mongo not found. creating local config file."
        )
        cfg = Config()
        write_config_to_file(cfg)
        logging.info(f"{CONFIG_FILE_NAME} created!")
        return 1


def read_config(count=1) -> Config:
    """Load the configuration defined by user."""
    if count > 3:
        logging.warning("Failed to read config, returning default config")
        return Config()
    if count != 1:
        logging.info(f"Trying to read config time:{count}")
    try:
        if stg.CONFIG_TYPE == 1:
            with open(CONFIG_FILE_NAME, encoding="utf8") as file:
                return Config.parse_raw(file.read())
        elif stg.CONFIG_TYPE == 2:
            return read_db()
        else:
            return Config()
    except Exception as err:
        logging.warning(err)
        stg.CONFIG_TYPE = detect_config_type()
        return read_config(count=count + 1)


def write_config(config: Config, persist=True):
    """Write changes in config back to file."""
    if stg.CONFIG_TYPE == 1 or stg.CONFIG_TYPE == 0:
        write_config_to_file(config)
    elif stg.CONFIG_TYPE == 2:
        if persist:
            update_db(config)


def get_env_var(name: str, optional: bool = False) -> str:
    """Fetch an env var."""
    var = os.getenv(name, "")

    while not var:
        if optional:
            return ""
        var = input(f"Enter {name}: ")
    return var


async def get_id(client: TelegramClient, peer):
    return await client.get_peer_id(peer)


async def load_from_to(
    client: TelegramClient, forwards: List[Forward]
) -> Dict[int, List[int]]:
    """Convert a list of Forward objects to a mapping."""
    from_to_dict = {}

    async def _(peer):
        return await get_id(client, peer)

    for forward in forwards:
        if not forward.use_this:
            continue
        source = forward.source
        if not isinstance(source, int) and source.strip() == "":
            continue
        src = await _(forward.source)
        from_to_dict[src] = [await _(dest) for dest in forward.dest]
    logging.info(f"From to dict is {from_to_dict}")
    return from_to_dict


async def load_admins(client: TelegramClient):
    for admin in CONFIG.admins:
        ADMINS.append(await get_id(client, admin))
    logging.info(f"Loaded admins are {ADMINS}")
    return ADMINS


def setup_mongo(client):
    mydb = client[MONGO_DB_NAME]
    mycol = mydb[MONGO_COL_NAME]
    if not mycol.find_one({"_id": 0}):
        mycol.insert_one({"_id": 0, "author": "nb", "config": Config().dict()})
    return mycol


def update_db(cfg):
    stg.mycol.update_one({"_id": 0}, {"$set": {"config": cfg.dict()}})


def read_db():
    obj = stg.mycol.find_one({"_id": 0})
    cfg = Config(**obj["config"])
    return cfg


PASSWORD = os.getenv("PASSWORD", "nb")
ADMINS = []

MONGO_CON_STR = os.getenv("MONGO_CON_STR")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "nb-config")
MONGO_COL_NAME = os.getenv("MONGO_COL_NAME", "nb-instance-0")

stg.CONFIG_TYPE = detect_config_type()
CONFIG = read_config()

if PASSWORD == "nb":
    logging.warning(
        "You have not set a password to protect the web access to nb.\n"
        "The default password `nb` is used."
    )

from_to = {}
comment_sources: Dict[int, int] = {}
comment_forward_map: Dict[int, "Forward"] = {}
is_bot: Optional[bool] = None
logging.info("config.py got executed")


def _clean_session_string(raw: str) -> str:
    """清理 session string，去除可能导致问题的字符。"""
    if not raw:
        return ""

    cleaned = raw.strip()

    # 去除首尾引号（单引号或双引号）
    for q in ('"', "'", '\u201c', '\u201d', '\u2018', '\u2019'):
        if cleaned.startswith(q) and cleaned.endswith(q):
            cleaned = cleaned[1:-1].strip()

    # 去除所有换行和多余空格
    cleaned = cleaned.replace("\n", "").replace("\r", "").replace(" ", "")

    return cleaned


def _validate_session_string(session_str: str) -> bool:
    """验证 session string 是否合法。"""
    if not session_str:
        return False

    # Telethon v1 session string 以 '1' 开头
    # 长度应该是 353 (IPv4) 或 369 (IPv6)（含版本前缀）
    if session_str[0] != '1':
        logging.error(
            f"Session string 版本不匹配: 首字符='{session_str[0]}' (期望 '1')\n"
            f"字符串长度={len(session_str)}\n"
            f"前10字符: '{session_str[:10]}...'"
        )
        return False

    expected_lengths = [353, 369]
    if len(session_str) not in expected_lengths:
        logging.warning(
            f"Session string 长度异常: {len(session_str)} "
            f"(期望 {expected_lengths})"
        )
        # 不直接 return False，让 Telethon 自己判断

    # 检查是否只包含 base64 字符
    import re
    if not re.match(r'^[A-Za-z0-9+/=]+$', session_str):
        invalid_chars = set(re.findall(r'[^A-Za-z0-9+/=]', session_str))
        logging.error(
            f"Session string 包含非法字符: {invalid_chars}\n"
            f"前20字符: '{session_str[:20]}...'"
        )
        return False

    return True


def get_SESSION(section: Any = None, default: str = "nb_bot"):
    """获取 Telethon session 对象。

    支持:
    - 用户账号: 使用 SESSION_STRING（StringSession）
    - Bot 账号: 使用 BOT_TOKEN（文件 session）
    """
    if section is None:
        section = CONFIG.login

    if section.SESSION_STRING and section.user_type == 1:
        # ★ 用户账号模式
        raw_string = section.SESSION_STRING

        # 也检查环境变量（可能比配置文件中的更新）
        env_session = os.getenv("SESSION_STRING", "")
        if env_session:
            raw_string = env_session
            logging.info("使用环境变量中的 SESSION_STRING")

        # 清理
        cleaned = _clean_session_string(raw_string)

        if not cleaned:
            logging.error(
                "❌ SESSION_STRING 为空！\n"
                "请在 Telegram Login 页面设置，或设置环境变量 SESSION_STRING"
            )
            sys.exit(1)

        # 验证
        if not _validate_session_string(cleaned):
            logging.error(
                "❌ SESSION_STRING 格式无效！\n"
                f"清理后长度: {len(cleaned)}\n"
                f"首字符: '{cleaned[0] if cleaned else '?'}'\n"
                f"前20字符: '{cleaned[:20]}...'\n"
                "\n请重新生成 session string:\n"
                "  方法1: https://replit.com/@artai8/tg-login\n"
                "  方法2: pip install tg-login && tg-login"
            )
            sys.exit(1)

        try:
            SESSION = StringSession(cleaned)
            logging.info(
                f"✅ Session string 验证通过 (长度={len(cleaned)})"
            )
        except ValueError as e:
            logging.error(
                f"❌ Telethon 拒绝 session string: {e}\n"
                f"清理后长度: {len(cleaned)}\n"
                f"首字符: '{cleaned[0]}'\n"
                "\n这通常意味着:\n"
                "  1. Session string 被截断了（复制不完整）\n"
                "  2. Session string 是用不兼容版本的 Telethon 生成的\n"
                "  3. Session string 中混入了不可见字符\n"
                "\n请重新生成 session string"
            )
            sys.exit(1)
        except Exception as e:
            logging.error(f"❌ 创建 StringSession 时发生未知错误: {e}")
            sys.exit(1)

        return SESSION

    elif section.BOT_TOKEN and section.user_type == 0:
        # ★ Bot 模式
        bot_token = section.BOT_TOKEN.strip()

        # 也检查环境变量
        env_token = os.getenv("BOT_TOKEN", "")
        if env_token:
            bot_token = env_token.strip()
            logging.info("使用环境变量中的 BOT_TOKEN")

        if not bot_token:
            logging.error(
                "❌ BOT_TOKEN 为空！\n"
                "请在 Telegram Login 页面设置，或设置环境变量 BOT_TOKEN"
            )
            sys.exit(1)

        # 简单验证 bot token 格式: 数字:字母数字
        if ":" not in bot_token:
            logging.error(
                f"❌ BOT_TOKEN 格式无效: 缺少冒号\n"
                f"正确格式: 123456789:ABCdefGHIjklMNOpqrSTUvwxyz\n"
                f"当前值前20字符: '{bot_token[:20]}...'"
            )
            sys.exit(1)

        logging.info("using bot account")
        SESSION = default
        return SESSION

    else:
        # ★ 未配置登录信息
        login_type = "用户账号" if section.user_type == 1 else "Bot"
        needed = "SESSION_STRING" if section.user_type == 1 else "BOT_TOKEN"

        logging.error(
            f"❌ 登录信息未设置！\n"
            f"当前模式: {login_type}\n"
            f"缺少: {needed}\n"
            f"\n请在 Web UI → Telegram Login 页面配置，\n"
            f"或设置环境变量 {needed}"
        )
        sys.exit(1)
