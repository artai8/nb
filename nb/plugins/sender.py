import sys
from nb.plugins import NbMessage, NbPlugin
from nb.config import CONFIG, get_SESSION
from telethon import TelegramClient


class NbSender(NbPlugin):
    id_ = "sender"

    async def __ainit__(self):
        sender = TelegramClient(get_SESSION(CONFIG.plugins.sender, 'nb_sender'), CONFIG.login.API_ID, CONFIG.login.API_HASH)
        if self.data.user_type == 0:
            if not self.data.BOT_TOKEN:
                sys.exit()
            await sender.start(bot_token=self.data.BOT_TOKEN)
        else:
            await sender.start()
        self.sender = sender

    async def modify(self, tm):
        tm.client = self.sender
        if tm.file_type != "nofile":
            tm.new_file = await tm.get_file()
            tm.cleanup = True
        return tm
