"""Load all user defined config and env vars."""

import logging
import os
import sys
from typing import Any, Dict, List, Optional, Union

from dotenv import load_dotenv
try:
    from pydantic import BaseModel, Field, field_validator
except Exception:
    from pydantic import BaseModel, Field, validator as field_validator
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
    # ✅ v2 写法：使用 default_factory 防止可变对象副作用
    dest_discussion_groups: List[Union[int, str]] = Field(default_factory=list)

    only_media: bool = False
    include_text_comments: bool = True
    skip_bot_comments: bool = False
    skip_admin_comments: bool = False

    post_mapping_mode: str = "auto"
    manual_post_mapping: Dict[str, str] = Field(default_factory=dict)
    manual_post_mapping_raw: str = ""


class Forward(BaseModel):
    """Blueprint for the forward object."""

    con_name: str = ""
    use_this: bool = True
    source: Union[int, str] = ""
    dest: List[Union[int, str]] = Field(default_factory=list)
    offset: int = 0
    end: Optional[int] = None
    comments: CommentsConfig = Field(default_factory=CommentsConfig)
    bot_media_enabled: Optional[bool] = None
    auto_comment_trigger_enabled: Optional[bool] = None
    bot_media_keyword_trigger_enabled: Optional[bool] = None
    bot_media_pagination_mode: str = ""
    bot_media_pagination_keywords_raw: str = ""
    comment_keyword_prefixes_raw: str = ""
    comment_keyword_suffixes_raw: str = ""


class LiveSettings(BaseModel):
    """Settings to configure how nb operates in live mode."""

    sequential_updates: bool = False
    delete_sync: bool = False
    delete_on_edit: Optional[str] = ".deleteMe"


class PastSettings(BaseModel):
    """Configuration for past mode."""

    delay: int = 0

    # ✅ v2 写法：@field_validator + @classmethod
    @field_validator("delay")
    @classmethod
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


class BotMediaSettings(BaseModel):
    enabled: bool = True
    enable_keyword_trigger: bool = True
    enable_pagination: bool = True
    pagination_mode: str = "auto"
    pagination_keywords_raw: str = ""
    ignore_filter: bool = True
    force_forward_on_empty: bool = True
    poll_interval: float = 1.2
    wait_timeout: float = 12.0
    max_pages: int = 5
    recent_limit: int = 80
    comment_keyword_prefixes_raw: str = "评论区回复\n评论区发送\n在评论区回复\n在评论区发送"
    comment_keyword_suffixes_raw: str = "获取资源\n领取\n获取\n得到内容"


class Config(BaseModel):
    """The blueprint for nb's whole config."""

    pid: int = 0
    theme: str = "light"
    login: LoginConfig = Field(default_factory=LoginConfig)
    admins: List[Union[int, str]] = Field(default_factory=list)
    forwards: List[Forward] = Field(default_factory=list)
    show_forwarded_from: bool = False
    mode: int = 0
    live: LiveSettings = Field(default_factory=LiveSettings)
    past: PastSettings = Field(default_factory=PastSettings)

    plugins: PluginConfig = Field(default_factory=PluginConfig)
    bot_messages: BotMessages = Field(default_factory=BotMessages)
    bot_media: BotMediaSettings = Field(default_factory=BotMediaSettings)


def write_config_to_file(config: Config):
    with open(CONFIG_FILE_NAME, "w", encoding="utf8") as file:
        dump_json = getattr(config, "model_dump_json", None)
        if callable(dump_json):
            file.write(dump_json(indent=4))
        else:
            file.write(config.json(indent=4))


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
                validate_json = getattr(Config, "model_validate_json", None)
                if callable(validate_json):
                    return validate_json(file.read())
                else:
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


async def load_forward_map(
    client: TelegramClient, forwards: List[Forward]
) -> Dict[int, Forward]:
    forward_map: Dict[int, Forward] = {}

    async def _(peer):
        return await get_id(client, peer)

    for forward in forwards:
        if not forward.use_this:
            continue
        source = forward.source
        if not isinstance(source, int) and source.strip() == "":
            continue
        src = await _(forward.source)
        forward_map[src] = forward
    return forward_map


async def load_admins(client: TelegramClient):
    for admin in CONFIG.admins:
        ADMINS.append(await get_id(client, admin))
    logging.info(f"Loaded admins are {ADMINS}")
    return ADMINS


def setup_mongo(client):
    mydb = client[MONGO_DB_NAME]
    mycol = mydb[MONGO_COL_NAME]
    if not mycol.find_one({"_id": 0}):
        model_dump = getattr(Config(), "model_dump", None)
        data = model_dump() if callable(model_dump) else Config().dict()
        mycol.insert_one({"_id": 0, "author": "nb", "config": data})
    return mycol


def update_db(cfg):
    model_dump = getattr(cfg, "model_dump", None)
    data = model_dump() if callable(model_dump) else cfg.dict()
    stg.mycol.update_one({"_id": 0}, {"$set": {"config": data}})


def read_db():
    obj = stg.mycol.find_one({"_id": 0})
    validate = getattr(Config, "model_validate", None)
    cfg = validate(obj["config"]) if callable(validate) else Config.parse_obj(obj["config"])
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
forward_map: Dict[int, "Forward"] = {}

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
