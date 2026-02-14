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

load_dotenv(os.path.join(os.getcwd(), ".env"))


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
        return max(0, min(val, 100))


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


def write_config_to_file(config):
    with open(CONFIG_FILE_NAME, "w", encoding="utf8") as f:
        f.write(config.json())


def detect_config_type():
    if MONGO_CON_STR:
        client = MongoClient(MONGO_CON_STR)
        stg.mycol = setup_mongo(client)
        return 2
    if CONFIG_FILE_NAME in os.listdir():
        return 1
    write_config_to_file(Config())
    return 1


def read_config(count=1):
    if count > 3:
        return Config()
    try:
        if stg.CONFIG_TYPE == 1:
            with open(CONFIG_FILE_NAME, encoding="utf8") as f:
                return Config.parse_raw(f.read())
        elif stg.CONFIG_TYPE == 2:
            return read_db()
        return Config()
    except Exception:
        stg.CONFIG_TYPE = detect_config_type()
        return read_config(count=count + 1)


def write_config(config, persist=True):
    if stg.CONFIG_TYPE in (0, 1):
        write_config_to_file(config)
    elif stg.CONFIG_TYPE == 2 and persist:
        update_db(config)


def _looks_like_bot_token(value):
    if not value:
        return False
    value = value.strip()
    if ":" in value and len(value) < 100:
        parts = value.split(":", 1)
        if parts[0].isdigit():
            return True
    return False


def _sync_env_to_config(cfg):
    modified = False
    env_map = {
        'API_ID': os.getenv("API_ID", ""),
        'API_HASH': os.getenv("API_HASH", ""),
        'SESSION_STRING': os.getenv("SESSION_STRING", ""),
        'BOT_TOKEN': os.getenv("BOT_TOKEN", ""),
    }
    if env_map['API_ID']:
        try:
            new_id = int(env_map['API_ID'])
            if cfg.login.API_ID != new_id:
                cfg.login.API_ID = new_id
                modified = True
        except ValueError:
            pass
    if env_map['API_HASH'] and cfg.login.API_HASH != env_map['API_HASH']:
        cfg.login.API_HASH = env_map['API_HASH']
        modified = True
    if env_map['SESSION_STRING'] and env_map['BOT_TOKEN']:
        if cfg.login.SESSION_STRING != env_map['SESSION_STRING']:
            cfg.login.SESSION_STRING = env_map['SESSION_STRING']
            modified = True
        if cfg.login.user_type != 1:
            cfg.login.user_type = 1
            modified = True
        if cfg.login.BOT_TOKEN != env_map['BOT_TOKEN']:
            cfg.login.BOT_TOKEN = env_map['BOT_TOKEN']
            modified = True
    elif env_map['SESSION_STRING']:
        if cfg.login.SESSION_STRING != env_map['SESSION_STRING']:
            cfg.login.SESSION_STRING = env_map['SESSION_STRING']
            modified = True
        if cfg.login.user_type != 1:
            cfg.login.user_type = 1
            modified = True
    elif env_map['BOT_TOKEN']:
        if cfg.login.BOT_TOKEN != env_map['BOT_TOKEN']:
            cfg.login.BOT_TOKEN = env_map['BOT_TOKEN']
            modified = True
        if cfg.login.user_type != 0:
            cfg.login.user_type = 0
            modified = True
    return modified


async def get_id(client, peer):
    if isinstance(peer, str):
        peer = peer.strip()
        if not peer:
            raise ValueError("peer为空")
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
        return entity.id
    except ValueError:
        if isinstance(peer, int):
            candidates = {peer}
            if peer > 0:
                candidates.update({int(f"-100{peer}"), -peer})
            peer_str = str(abs(peer))
            if peer_str.startswith("100") and len(peer_str) > 3:
                candidates.update({int(peer_str[3:]), -int(peer_str[3:])})
            for c in candidates:
                if c == peer:
                    continue
                try:
                    entity = await client.get_entity(c)
                    return entity.id
                except Exception:
                    continue
        raise
    except Exception:
        raise


async def load_from_to(client, forwards):
    from_to_dict = {}
    for forward in forwards:
        if not forward.use_this:
            continue
        if not isinstance(forward.source, int) and str(forward.source).strip() == "":
            continue
        try:
            src = await get_id(client, forward.source)
        except Exception:
            continue
        dest_ids = []
        for dest in forward.dest:
            try:
                dest_ids.append(await get_id(client, dest))
            except Exception:
                continue
        if dest_ids:
            from_to_dict[src] = dest_ids
    return from_to_dict


async def load_admins(client):
    for admin in CONFIG.admins:
        try:
            ADMINS.append(await get_id(client, admin))
        except Exception:
            pass
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
    return Config(**obj["config"])


PASSWORD = os.getenv("PASSWORD", "nb")
ADMINS = []
MONGO_CON_STR = os.getenv("MONGO_CON_STR")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "nb-config")
MONGO_COL_NAME = os.getenv("MONGO_COL_NAME", "nb-instance-0")

stg.CONFIG_TYPE = detect_config_type()
CONFIG = read_config()
if _sync_env_to_config(CONFIG):
    write_config(CONFIG)

from_to = {}
comment_sources: Dict[int, int] = {}
comment_forward_map: Dict[int, "Forward"] = {}
is_bot: Optional[bool] = None
source_to_forward: Dict[int, "Forward"] = {}


def get_SESSION(section=None, default="nb_bot"):
    if section is None:
        section = CONFIG.login
    if section.user_type == 1:
        if section.SESSION_STRING:
            if _looks_like_bot_token(section.SESSION_STRING):
                sys.exit(1)
            return StringSession(section.SESSION_STRING)
        sys.exit(1)
    if section.user_type == 0:
        if section.BOT_TOKEN:
            return default
        if section.SESSION_STRING:
            if _looks_like_bot_token(section.SESSION_STRING):
                sys.exit(1)
            return StringSession(section.SESSION_STRING)
        sys.exit(1)
    sys.exit(1)
