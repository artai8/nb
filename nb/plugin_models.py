from enum import Enum
from typing import Dict, List

from pydantic import BaseModel, ConfigDict, Field
try:
    from watermark import Position
except Exception:
    class Position(str, Enum):
        centre = "centre"
        top_left = "top_left"
        top_right = "top_right"
        bottom_left = "bottom_left"
        bottom_right = "bottom_right"


class FileType(str, Enum):
    AUDIO = "audio"
    GIF = "gif"
    VIDEO = "video"
    VIDEO_NOTE = "video_note"
    STICKER = "sticker"
    CONTACT = "contact"
    PHOTO = "photo"
    DOCUMENT = "document"
    NOFILE = "nofile"


class FilterList(BaseModel):
    blacklist: List[str] = Field(default_factory=list)
    whitelist: List[str] = Field(default_factory=list)


class FilesFilterList(BaseModel):
    blacklist: List[FileType] = Field(default_factory=list)
    whitelist: List[FileType] = Field(default_factory=list)


class TextFilter(FilterList):
    case_sensitive: bool = False
    regex: bool = False


class Style(str, Enum):
    BOLD = "bold"
    ITALICS = "italics"
    ITALIC = "italic"
    CODE = "code"
    STRIKE = "strike"
    STRIKETHROUGH = "strikethrough"
    SPOILER = "spoiler"
    PLAIN = "plain"
    PRESERVE = "preserve"


STYLE_CODES = {
    "bold": "**",
    "italics": "__",
    "italic": "__",
    "code": "`",
    "strike": "~~",
    "strikethrough": "~~",
    "spoiler": "||",
    "plain": "",
}


class Filters(BaseModel):
    check: bool = False
    users: FilterList = Field(default_factory=FilterList)
    files: FilesFilterList = Field(default_factory=FilesFilterList)
    text: TextFilter = Field(default_factory=TextFilter)


class Format(BaseModel):
    model_config = ConfigDict(validate_assignment=True)
    check: bool = False
    style: Style = Style.PRESERVE


class MarkConfig(BaseModel):
    check: bool = False
    image: str = "image.png"
    position: str = "bottom_right"
    # Re-encode video/audio and repack zip so forwarded copies get a new
    # file_unique_id. Video watermark (video_mark) rides on the same pass.
    recode: bool = True
    video_mark: bool = False
    # Video ads: prepend/append/insert a video clip (uploaded per task).
    # ad_midroll_at is seconds from the start; 0 means the middle.
    ad_intro: bool = False
    ad_midroll: bool = False
    ad_outro: bool = False
    ad_midroll_at: float = 0
    # Runtime paths, filled by the worker from the uploaded assets.
    ad_intro_path: str = ""
    ad_midroll_path: str = ""
    ad_outro_path: str = ""


class OcrConfig(BaseModel):
    check: bool = False
    lang: str = "chi_sim"


class Replace(BaseModel):
    check: bool = False
    text: Dict[str, str] = Field(default_factory=dict)
    text_raw: str = ""
    regex: bool = False


class Caption(BaseModel):
    check: bool = False
    header: str = ""
    footer: str = ""


class SpoilerConfig(BaseModel):
    check: bool = False


class InlineButtonMode(str, Enum):
    REMOVE = "remove"
    REPLACE_URL = "replace_url"
    REPLACE_ALL = "replace_all"


class InlineButtonConfig(BaseModel):
    model_config = ConfigDict(validate_assignment=True)
    check: bool = False
    mode: InlineButtonMode = InlineButtonMode.REMOVE
    url_replacements: Dict[str, str] = Field(default_factory=dict)
    url_replacements_raw: str = ""
    text_replacements: Dict[str, str] = Field(default_factory=dict)
    text_replacements_raw: str = ""


class PluginConfig(BaseModel):
    filter: Filters = Field(default_factory=Filters)
    fmt: Format = Field(default_factory=Format)
    mark: MarkConfig = Field(default_factory=MarkConfig)
    ocr: OcrConfig = Field(default_factory=OcrConfig)
    replace: Replace = Field(default_factory=Replace)
    caption: Caption = Field(default_factory=Caption)
    spoiler: SpoilerConfig = Field(default_factory=SpoilerConfig)
    inline: InlineButtonConfig = Field(default_factory=InlineButtonConfig)


ASYNC_PLUGIN_IDS = ["ocr", "mark"]
