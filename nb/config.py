# nb/config.py

import logging
import os
import sys
import re
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

class Forward(BaseModel):
    con_name: str = ""
    use_this: bool = True
    source: Union[int, str] = ""
    dest: List[Union[int, str]] = []
    offset: int = 0
    end: Optional[int] = None
    forward_comments: bool = False
    comm_only_media: bool = False
    comm_max_text: int = 5

class LiveSettings(BaseModel):
    sequential_updates: bool = False
    delete_sync: bool = False
    delete_on_edit: Optional[str] = ".deleteMe"

class PastSettings(BaseModel):
    delay: int = 0
    @validator("delay")
    def validate_delay(cls, val):
        return max(0, min(100, val))

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
    if os.getenv("MONGO_CON_STR"): return 2
    if CONFIG_FILE_NAME in os.listdir(): return 1
    cfg = Config()
    write_config_to_file(cfg)
    return 1

def read_config(count=1) -> Config:
    try:
        if stg.CONFIG_TYPE == 1:
            with open(CONFIG_FILE_NAME, encoding="utf8") as file:
                return Config.parse_raw(file.read())
        elif stg.CONFIG_TYPE == 2:
            return read_db()
        return Config()
    except Exception as e:
        return Config()

def write_config(config: Config, persist=True):
    if stg.CONFIG_TYPE in [0, 1]:
        write_config_to_file(config)
    elif stg.CONFIG_TYPE == 2 and persist:
        update_db(config)

async def get_id(client: TelegramClient, peer):
    """支持数字 ID, @username, 和 t.me 链接"""
    if not peer: return None
    if isinstance(peer, int): return peer
    
    peer_str = str(peer).strip()
    # 解析链接
    if "t.me/" in peer_str:
        peer_str = peer_str.split('/')[-1]
        if peer_str.isdigit() or (peer_str.startswith('-100') and peer_str[4:].isdigit()):
            pass # 已经是 ID 格式
        elif "t.me/c/" in str(peer):
            # 处理私有频道链接 https://t.me/c/12345678/123
            try:
                parts = str(peer).split('/')
                peer_str = f"-100{parts[parts.index('c')+1]}"
            except: pass

    # 尝试纯数字转换
    try:
        return int(peer_str)
    except:
        pass

    # 尝试通过 Telethon 解析
    try:
        entity = await client.get_entity(peer_str)
        # ✅ 关键修复：添加 await
        return await client.get_peer_id(entity)
    except Exception as e:
        logging.error(f"❌ 解析 ID 失败 '{peer}': {e}")
        return None

async def load_from_to(client: TelegramClient, forwards: List[Forward]) -> Dict[int, List[int]]:
    from_to_dict = {}
    for f in forwards:
        if not f.use_this: continue
        sid = await get_id(client, f.source)
        if sid:
            dests = []
            for d in f.dest:
                did = await get_id(client, d)
                if did: dests.append(did)
            if dests: from_to_dict[sid] = dests
    return from_to_dict

async def load_admins(client: TelegramClient):
    admins = []
    for a in CONFIG.admins:
        aid = await get_id(client, a)
        if aid: admins.append(aid)
    return admins

PASSWORD = os.getenv("PASSWORD", "nb")
MONGO_CON_STR = os.getenv("MONGO_CON_STR")
stg.CONFIG_TYPE = detect_config_type()
CONFIG = read_config()
ADMINS = []
from_to = {}
is_bot = None

def get_SESSION(section=None, default="nb_bot"):
    sec = section or CONFIG.login
    if sec.SESSION_STRING and sec.user_type == 1:
        return StringSession(sec.SESSION_STRING)
    return default
