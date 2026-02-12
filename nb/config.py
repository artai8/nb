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
    """解析 peer 并确保实体被缓存（含 access_hash）。

    支持的输入格式：
    - 数字 ID（int）：如 -1001234567890
    - 用户名（str）：如 "@channel_name" 或 "channel_name"
    - t.me 链接（str）：如 "https://t.me/channel_name"
    """
    # ★ 预处理：如果是字符串，清理格式
    if isinstance(peer, str):
        peer = peer.strip()
        if not peer:
            raise ValueError("peer 为空字符串")

        # t.me 链接转为用户名
        if "t.me/" in peer:
            parts = peer.split("t.me/")
            if len(parts) == 2:
                name = parts[1].strip().rstrip("/")
                if name and not name.startswith("+"):
                    peer = f"@{name}" if not name.startswith("@") else name

        # 尝试将纯数字字符串转为 int
        try:
            peer = int(peer)
        except ValueError:
            pass

    try:
        # ★ 关键：用 get_entity 而不是 get_peer_id
        # get_entity 会完整解析并缓存实体（包含 access_hash）
        entity = await client.get_entity(peer)
        logging.info(f"✅ 解析实体成功: {peer} → id={entity.id}")
        return entity.id
    except ValueError:
        # 如果是纯数字 ID 且 get_entity 失败，尝试不同的格式
        if isinstance(peer, int):
            candidates = set()
            candidates.add(peer)
            # 可能缺少 -100 前缀
            if peer > 0:
                candidates.add(int(f"-100{peer}"))
                candidates.add(-peer)
            # 可能多了 -100 前缀
            peer_str = str(abs(peer))
            if peer_str.startswith("100") and len(peer_str) > 3:
                candidates.add(int(peer_str[3:]))
                candidates.add(-int(peer_str[3:]))

            for candidate in candidates:
                if candidate == peer:
                    continue  # 已经试过了
                try:
                    entity = await client.get_entity(candidate)
                    logging.info(
                        f"✅ 通过候选 ID {candidate} 解析成功: "
                        f"{peer} → id={entity.id}"
                    )
                    return entity.id
                except Exception:
                    continue

        logging.error(
            f"❌ 无法解析实体 '{peer}'\n"
            f"💡 建议:\n"
            f"   - 使用 @用户名 格式（如 @mychannel）\n"
            f"   - 或使用 https://t.me/mychannel 链接\n"
            f"   - 确保账号已加入该频道/群组"
        )
        raise
    except Exception as e:
        logging.error(f"❌ 无法解析实体 '{peer}': {e}")
        raise


async def load_from_to(
    client: TelegramClient, forwards: List[Forward]
) -> Dict[int, List[int]]:
    """Convert a list of Forward objects to a mapping.

    关键改进：
    1. 使用 get_entity 确保实体完整缓存
    2. 跳过无法解析的源/目标，而不是整体崩溃
    3. 详细的错误日志
    """
    from_to_dict = {}

    for forward in forwards:
        if not forward.use_this:
            continue

        source = forward.source
        if not isinstance(source, int) and str(source).strip() == "":
            logging.warning(f"⚠️ 连接 '{forward.con_name}' 源为空，跳过")
            continue

        # ——— 解析源 ———
        try:
            src = await get_id(client, forward.source)
        except Exception as e:
            logging.error(
                f"❌ 无法解析源 '{forward.source}' "
                f"(连接: {forward.con_name}): {e}\n"
                f"💡 请确认账号已加入该频道/群组，或使用 @用户名 格式"
            )
            continue

        # ——— 解析目标 ———
        dest_ids = []
        for dest in forward.dest:
            try:
                d = await get_id(client, dest)
                dest_ids.append(d)
            except Exception as e:
                logging.error(
                    f"❌ 无法解析目标 '{dest}' "
                    f"(连接: {forward.con_name}): {e}\n"
                    f"💡 请确认账号已加入该频道/群组，或使用 @用户名 格式"
                )
                continue

        if dest_ids:
            from_to_dict[src] = dest_ids
            logging.info(
                f"✅ 连接 '{forward.con_name}': {src} → {dest_ids}"
            )
        else:
            logging.warning(
                f"⚠️ 连接 '{forward.con_name}' 没有有效的目标，跳过"
            )

    logging.info(f"📋 最终转发映射: {from_to_dict}")

    if not from_to_dict:
        logging.warning(
            "⚠️ 没有任何有效的转发连接！\n"
            "💡 常见原因:\n"
            "   1. 账号未加入源/目标频道或群组\n"
            "   2. 频道/群组 ID 不正确（建议使用 @用户名）\n"
            "   3. 使用 Bot 账号但 Bot 未被添加到群组\n"
            "   4. 私有频道需要先手动加入"
        )

    return from_to_dict


async def load_admins(client: TelegramClient):
    for admin in CONFIG.admins:
        try:
            admin_id = await get_id(client, admin)
            ADMINS.append(admin_id)
        except Exception as e:
            logging.error(f"❌ 无法解析管理员 '{admin}': {e}")
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


def get_SESSION(section: Any = None, default: str = "nb_bot"):
    if section is None:
        section = CONFIG.login
    if section.SESSION_STRING and section.user_type == 1:
        logging.info("using session string")
        SESSION = StringSession(section.SESSION_STRING)
    elif section.BOT_TOKEN and section.user_type == 0:
        logging.info("using bot account")
        SESSION = default
    else:
        logging.warning("Login information not set!")
        sys.exit()
    return SESSION
