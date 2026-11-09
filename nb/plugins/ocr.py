"""Tesseract OCR image text extraction plugin."""

import pytesseract
from PIL import Image

from nb.multitenant.media_pool import run_cpu
from nb.plugins import NbMessage, NbPlugin
from nb.utils import cleanup


def _ocr_file(path: str, lang: str) -> str:
    with Image.open(path) as image:
        return pytesseract.image_to_string(image, lang=lang)


class NbOcr(NbPlugin):
    id_ = "ocr"

    def __init__(self, data) -> None:
        self.data = data

    async def modify(self, tm: NbMessage) -> NbMessage:
        if not self.data.check or tm.file_type not in ["photo"]:
            return tm
        file = await tm.get_file()
        if not file:
            return tm
        lang = getattr(self.data, "lang", "chi_sim")
        try:
            extracted = await run_cpu(_ocr_file, file, lang)
            if extracted and not tm.text:
                tm.text = extracted.strip()
        except Exception as e:
            if not tm.text:
                tm.text = f"OCR Error: {e}"
        finally:
            cleanup(file)
        return tm


OCRPlugin = NbOcr
