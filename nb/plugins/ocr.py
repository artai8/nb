import pytesseract
from PIL import Image
from nb.plugins import NbMessage, NbPlugin
from nb.utils import cleanup


class NbOcr(NbPlugin):
    id_ = "ocr"

    def __init__(self, data):
        pass

    async def modify(self, tm):
        if tm.file_type != "photo":
            return tm
        f = await tm.get_file()
        tm.text = pytesseract.image_to_string(Image.open(f))
        cleanup(f)
        return tm
