import pytesseract
from PIL import Image

from nb.plugins import NbMessage, NbPlugin
from nb.utils import cleanup


class NbOcr(NbPlugin):
    id_ = "ocr"

    def __init__(self, data) -> None:
        self.data = data

    async def modify(self, tm: NbMessage) -> NbMessage:

        if not tm.file_type in ["photo"]:
            return tm

        file = await tm.get_file()
        lang = getattr(self.data, "lang", "chi_sim")
        try:
            tm.text = pytesseract.image_to_string(Image.open(file), lang=lang)
        except Exception as e:
            tm.text = f"OCR Error: {e}"
        cleanup(file)
        return tm
