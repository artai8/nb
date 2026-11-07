"""Load user defined config and environment variables."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

from nb.const import CONFIG_FILE_NAME
from nb.plugin_models import PluginConfig

load_dotenv(os.path.join(os.getcwd(), ".env"))

PASSWORD = os.getenv("PASSWORD", "admin")
ADMINS: List[int] = [
    int(x.strip()) for x in os.getenv("ADMINS", "").split(",") if x.strip().isdigit()
]
is_bot: Optional[bool] = False


class CommentsConfig(BaseModel):
    enabled: bool = False
    source_mode: str = "comments"
    source_discussion_group: Optional[Union[int, str]] = None
    dest_mode: str = "comments"
    dest_discussion_groups: List[Union[int, str]] = Field(default_factory=list)
    only_media: bool = False
    include_text_comments: bool = True
    skip_bot_comments: bool = False
    skip_admin_comments: bool = False
    post_mapping_mode: str = "auto"
    manual_post_mapping: Dict[str, str] = Field(default_factory=dict)
    manual_post_mapping_raw: str = ""
    merge_comment_media: bool = False
    merge_wait_seconds: int = 60


class Forward(BaseModel):
    con_name: str = ""
    use_this: bool = True
    sources: List[Union[int, str]] = Field(default_factory=list)
    dest: List[Union[int, str]] = Field(default_factory=list)
    offset: int = 0
    offsets: Dict[str, int] = Field(default_factory=dict)
    end: Optional[int] = None
    comments: CommentsConfig = Field(default_factory=CommentsConfig)
    daily_limit: int = 0


def read_config(path: str = CONFIG_FILE_NAME) -> Dict[str, Any]:
    file_path = Path(path)
    if not file_path.exists():
        return {"password": PASSWORD, "connections": [], "plugins": {}}
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logging.warning("Failed to read %s: %s", path, exc)
        return {"password": PASSWORD, "connections": [], "plugins": {}}


def write_config(data: Dict[str, Any], path: str = CONFIG_FILE_NAME) -> None:
    file_path = Path(path)
    file_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


CONFIG = read_config()
