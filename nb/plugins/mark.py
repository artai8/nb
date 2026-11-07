"""Image and video PNG overlay watermark plugin."""

import os
from PIL import Image
from nb.multitenant.media_pool import run_cpu
from nb.plugins import NbMessage, NbPlugin


def _apply_image_watermark(base_path: str, watermark_path: str, position: str) -> str:
    out_path = f"{base_path}.watermarked.png"
    with Image.open(base_path).convert("RGBA") as base_img, Image.open(watermark_path).convert("RGBA") as wm_img:
        bw, bh = base_img.size
        max_wm_w = max(32, bw // 4)
        if wm_img.width > max_wm_w:
            ratio = max_wm_w / float(wm_img.width)
            wm_img = wm_img.resize((max_wm_w, max(16, int(wm_img.height * ratio))))
        ww, wh = wm_img.size
        pad = 16
        pos_map = {
            "top_left": (pad, pad),
            "top_right": (max(0, bw - ww - pad), pad),
            "bottom_left": (pad, max(0, bh - wh - pad)),
            "bottom_right": (max(0, bw - ww - pad), max(0, bh - wh - pad)),
            "centre": (max(0, (bw - ww) // 2), max(0, (bh - wh) // 2)),
        }
        xy = pos_map.get(str(position), pos_map["bottom_right"])
        layer = Image.new("RGBA", base_img.size, (0, 0, 0, 0))
        layer.paste(wm_img, xy, wm_img)
        combined = Image.alpha_composite(base_img, layer)
        combined.save(out_path, format="PNG")
    return out_path


class NbMark(NbPlugin):
    id_ = "mark"

    def __init__(self, data) -> None:
        self.data = data

    async def modify(self, tm: NbMessage) -> NbMessage:
        if not self.data.check or tm.file_type not in {"photo"}:
            return tm
        wm_path = self.data.image
        if not wm_path or not os.path.exists(wm_path):
            return tm
        downloaded = await tm.get_file()
        if not downloaded:
            return tm
        try:
            tm.new_file = await run_cpu(_apply_image_watermark, downloaded, wm_path, str(self.data.position))
        finally:
            if os.path.exists(downloaded):
                try:
                    os.remove(downloaded)
                except OSError:
                    pass
        return tm


MarkPlugin = NbMark
