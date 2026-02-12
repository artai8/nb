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
    con_name: str = ""
    use_this: bool = True
    source: Union[int, str] = ""
    dest: List[Union[int, str]] = []
    offset: int = 0
    end: Optional[int] = None
    comments: CommentsConfig = CommentsConfig()


class LiveSettings(BaseModel):
    sequential_updates: bool = False
    delete_sync: bool = False
    delete_on_edit: Optional[str] = ".deleteMe"


class PastSettings(BaseModel):
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
    user_type: int = 0
    phone_no: int = 91
    USERNAME: str = ""
    SESSION_STRING: str = ""
    BOT_TOKEN: str = ""


class BotMessages(BaseModel):
    start: str = "Hi! I am alive"
    bot_help: str = "For details visit github.com/artai8/nb"


class Config(BaseModel):
    pid: int = 0
    theme: str = "light"
    login: LoginConfig = LoginConfig()
    admins: List[Union[int, str]] = []
    forwards: List[Forward] = []
    show_forwarded_from: bool = False
    mode: int = 0
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
    if stg.CONFIG_TYPE == 1 or stg.CONFIG_TYPE == 0:
        write_config_to_file(config)
    elif stg.CONFIG_TYPE == 2:
        if persist:
            update_db(config)


def get_env_var(name: str, optional: bool = False) -> str:
    var = os.getenv(name, "")
    while not var:
        if optional:
            return ""
        var = input(f"Enter {name}: ")
    return var


def _looks_like_bot_token(value: str) -> bool:
    if not value:
        return False
    value = value.strip()
    if ":" in value and len(value) < 100:
        parts = value.split(":", 1)
        if parts[0].isdigit():
            return True
    return False


def _sync_env_to_config(cfg: Config) -> bool:
    modified = False

    env_api_id = os.getenv("API_ID", "")
    env_api_hash = os.getenv("API_HASH", "")
    env_session_string = os.getenv("SESSION_STRING", "")
    env_bot_token = os.getenv("BOT_TOKEN", "")

    if env_api_id:
        try:
            new_id = int(env_api_id)
            if cfg.login.API_ID != new_id:
                cfg.login.API_ID = new_id
                modified = True
        except ValueError:
            logging.warning(f"环境变量 API_ID 不是整数: {env_api_id}")

    if env_api_hash and cfg.login.API_HASH != env_api_hash:
        cfg.login.API_HASH = env_api_hash
        modified = True

    if env_session_string and env_bot_token:
        if cfg.login.SESSION_STRING != env_session_string:
            cfg.login.SESSION_STRING = env_session_string
            modified = True
        if cfg.login.user_type != 1:
            cfg.login.user_type = 1
            modified = True
        if cfg.login.BOT_TOKEN != env_bot_token:
            cfg.login.BOT_TOKEN = env_bot_token
            modified = True
    elif env_session_string:
        if cfg.login.SESSION_STRING != env_session_string:
            cfg.login.SESSION_STRING = env_session_string
            modified = True
        if cfg.login.user_type != 1:
            cfg.login.user_type = 1
            modified = True
    elif env_bot_token:
        if cfg.login.BOT_TOKEN != env_bot_token:
            cfg.login.BOT_TOKEN = env_bot_token
            modified = True
        if cfg.login.user_type != 0:
            cfg.login.user_type = 0
            modified = True

    return modified


async def get_id(client: TelegramClient, peer):
    if isinstance(peer, str):
        peer = peer.strip()
        if not peer:
            raise ValueError("peer 为空字符串")

        if "t.me/" in peer:
            parts = peer.split("t.me/")
            if len(parts) == 2:
                name = parts[1].strip().rstrip("/")
                if name and not name.startswith("+"):
                    peer = f"@{name}" if not name.startswith("@") else name

        try:
            peer = int(peer)
        except ValueError:
            pass

    try:
        entity = await client.get_entity(peer)
        logging.info(f"✅ 解析实体成功: {peer} → id={entity.id}")
        return entity.id
    except ValueError:
        if isinstance(peer, int):
            candidates = set()
            candidates.add(peer)
            if peer > 0:
                candidates.add(int(f"-100{peer}"))
                candidates.add(-peer)
            peer_str = str(abs(peer))
            if peer_str.startswith("100") and len(peer_str) > 3:
                candidates.add(int(peer_str[3:]))
                candidates.add(-int(peer_str[3:]))

            for candidate in candidates:
                if candidate == peer:
                    continue
                try:
                    entity = await client.get_entity(candidate)
                    logging.info(
                        f"✅ 通过候选 ID {candidate} 解析成功: "
                        f"{peer} → id={entity.id}"
                    )
                    return entity.id
                except Exception:
                    continue

        logging.error(f"❌ 无法解析实体 '{peer}'")
        raise
    except Exception as e:
        logging.error(f"❌ 无法解析实体 '{peer}': {e}")
        raise


async def load_from_to(
    client: TelegramClient, forwards: List[Forward]
) -> Dict[int, List[int]]:
    from_to_dict = {}

    for forward in forwards:
        if not forward.use_this:
            continue

        source = forward.source
        if not isinstance(source, int) and str(source).strip() == "":
            logging.warning(f"⚠️ 连接 '{forward.con_name}' 源为空，跳过")
            continue

        try:
            src = await get_id(client, forward.source)
        except Exception as e:
            logging.error(
                f"❌ 无法解析源 '{forward.source}' "
                f"(连接: {forward.con_name}): {e}"
            )
            continue

        dest_ids = []
        for dest in forward.dest:
            try:
                d = await get_id(client, dest)
                dest_ids.append(d)
            except Exception as e:
                logging.error(
                    f"❌ 无法解析目标 '{dest}' "
                    f"(连接: {forward.con_name}): {e}"
                )
                continue

        if dest_ids:
            from_to_dict[src] = dest_ids
            logging.info(f"✅ 连接 '{forward.con_name}': {src} → {dest_ids}")
        else:
            logging.warning(f"⚠️ 连接 '{forward.con_name}' 没有有效的目标，跳过")

    logging.info(f"📋 最终转发映射: {from_to_dict}")

    if not from_to_dict:
        logging.warning("⚠️ 没有任何有效的转发连接！")

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

_env_modified = _sync_env_to_config(CONFIG)
if _env_modified:
    write_config(CONFIG)

if PASSWORD == "nb":
    logging.warning(
        "You have not set a password to protect the web access to nb.\n"
        "The default password `nb` is used."
    )

from_to = {}
comment_sources: Dict[int, int] = {}
comment_forward_map: Dict[int, "Forward"] = {}
is_bot: Optional[bool] = None

# ★ 新增：源频道ID → Forward 对象的映射，用于 past 模式评论区
source_to_forward: Dict[int, "Forward"] = {}


def get_SESSION(section: Any = None, default: str = "nb_bot"):
    if section is None:
        section = CONFIG.login

    if section.user_type == 1:
        if section.SESSION_STRING:
            if _looks_like_bot_token(section.SESSION_STRING):
                logging.error(
                    "❌ SESSION_STRING 字段中的值看起来是 Bot Token！"
                )
                sys.exit(1)
            return StringSession(section.SESSION_STRING)
        logging.error("❌ 账号类型为 User 但 Session String 为空！")
        sys.exit(1)

    if section.user_type == 0:
        if section.BOT_TOKEN:
            return default
        if section.SESSION_STRING:
            logging.warning("⚠️ 账号类型为 Bot 但没有 Bot Token，尝试使用 Session String。")
            if _looks_like_bot_token(section.SESSION_STRING):
                logging.error("❌ Session String 字段的值像 Bot Token")
                sys.exit(1)
            return StringSession(section.SESSION_STRING)
        logging.error("❌ 账号类型为 Bot 但 Bot Token 为空！")
        sys.exit(1)

    logging.error(f"❌ 未知的 user_type: {section.user_type}")
    sys.exit(1)
