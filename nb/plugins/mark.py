import os
import shutil
import requests
from watermark import File, Watermark, apply_watermark
from nb.plugins import NbMessage, NbPlugin
from nb.utils import cleanup


def download_image(url, filename="image.png"):
    if filename in os.listdir():
        return True
    try:
        response = requests.get(url, stream=True)
        if response.status_code == 200:
            with open(filename, "wb") as f:
                response.raw.decode_content = True
                shutil.copyfileobj(response.raw, f)
            return True
    except Exception:
        return False


class NbMark(NbPlugin):
    id_ = "mark"

    def __init__(self, data):
        self.data = data

    async def modify(self, tm):
        if tm.file_type not in ("gif", "video", "photo"):
            return tm
        downloaded = await tm.get_file()
        base = File(downloaded)
        if self.data.image.startswith("https://"):
            download_image(self.data.image)
            overlay = File("image.png")
        else:
            overlay = File(self.data.image)
        wtm = Watermark(overlay, self.data.position)
        tm.new_file = apply_watermark(base, wtm, frame_rate=self.data.frame_rate)
        cleanup(downloaded)
        tm.cleanup = True
        return tm

    async def modify_group(self, tms):
        for tm in tms:
            if tm.file_type in ("gif", "video", "photo"):
                await self.modify(tm)
        return tms
