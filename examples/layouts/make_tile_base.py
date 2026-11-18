"""一次性脚本：生成 assets/tile_base.png（象牙色圆角牌底，四角透明）。"""
from pathlib import Path

from PIL import Image, ImageDraw

W, H, R = 400, 560, 48
img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([0, 0, W - 1, H - 1], radius=R, fill=(176, 160, 128, 255))
d.rounded_rectangle([6, 6, W - 7, H - 7], radius=R - 4, fill=(250, 245, 228, 255))
out = Path(__file__).parent / "assets" / "tile_base.png"
img.save(out)
print(out)
