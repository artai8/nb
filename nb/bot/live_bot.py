import logging
import yaml
from telethon import events
from nb import config
from nb.bot.utils import admin_protect, display_forwards, get_args, get_command_prefix, remove_source
from nb.config import CONFIG, write_config
from nb.plugin_models import Style


@admin_protect
async def forward_command_handler(event):
    notes = "用法: /forward source: a\ndest: [b,c]"
    try:
        args = get_args(event.message.text)
        if not args:
            raise ValueError(f"{notes}\n{display_forwards(CONFIG.forwards)}")
        parsed = yaml.safe_load(args)
        forward = config.Forward(**parsed)
        try:
            remove_source(forward.source, CONFIG.forwards)
        except Exception:
            pass
        CONFIG.forwards.append(forward)
        config.from_to = await config.load_from_to(event.client, CONFIG.forwards)
        await event.respond("成功")
        write_config(CONFIG)
    except ValueError as err:
        await event.respond(str(err))
    finally:
        raise events.StopPropagation


@admin_protect
async def remove_command_handler(event):
    notes = "用法: /remove source: -100"
    try:
        args = get_args(event.message.text)
        if not args:
            raise ValueError(f"{notes}\n{display_forwards(CONFIG.forwards)}")
        parsed = yaml.safe_load(args)
        CONFIG.forwards = remove_source(parsed.get("source"), CONFIG.forwards)
        config.from_to = await config.load_from_to(event.client, CONFIG.forwards)
        await event.respond("成功")
        write_config(CONFIG)
    except ValueError as err:
        await event.respond(str(err))
    finally:
        raise events.StopPropagation


@admin_protect
async def style_command_handler(event):
    try:
        args = get_args(event.message.text)
        if not args:
            raise ValueError("用法: /style bold\n选项: preserve,plain,bold,italics,code,strike")
        valid = [i.value for i in Style]
        if args not in valid:
            raise ValueError(f"无效样式，可选: {valid}")
        CONFIG.plugins.fmt.style = args
        await event.respond("成功")
        write_config(CONFIG)
    except ValueError as err:
        await event.respond(str(err))
    finally:
        raise events.StopPropagation


async def start_command_handler(event):
    await event.respond(CONFIG.bot_messages.start)


async def help_command_handler(event):
    await event.respond(CONFIG.bot_messages.bot_help)


def get_events():
    _ = get_command_prefix()
    return {
        "start": (start_command_handler, events.NewMessage(pattern=f"{_}start")),
        "forward": (forward_command_handler, events.NewMessage(pattern=f"{_}forward")),
        "remove": (remove_command_handler, events.NewMessage(pattern=f"{_}remove")),
        "style": (style_command_handler, events.NewMessage(pattern=f"{_}style")),
        "help": (help_command_handler, events.NewMessage(pattern=f"{_}help")),
    }
