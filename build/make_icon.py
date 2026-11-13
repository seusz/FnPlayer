#!/usr/bin/env python3
"""生成 FnPlayer 应用图标（多尺寸 .ico）与占位海报 SVG。"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "app" / "web"


def make_icon(size: int = 512) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 渐变圆角底
    grad = Image.new("RGBA", (size, size))
    gd = ImageDraw.Draw(grad)
    for y in range(size):
        t = y / max(1, size - 1)
        r = int(255 - 20 * t)
        g = int(91 + 47 * t)
        b = int(74 - 20 * t)
        gd.line([(0, y), (size, y)], fill=(r, g, b, 255))
    radius = int(size * 0.23)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1),
                                           radius=radius, fill=255)
    img.paste(grad, (0, 0), mask)
    # 播放三角
    cx, cy = size * 0.44, size * 0.5
    w, h = size * 0.30, size * 0.36
    d.polygon([(cx - w / 2, cy - h / 2), (cx - w / 2, cy + h / 2),
               (cx + w / 2, cy)], fill=(255, 255, 255, 255))
    # 右下角小圆点，呼应"Player"
    dot = size * 0.075
    dx, dy = size * 0.78, size * 0.72
    d.ellipse((dx - dot, dy - dot, dx + dot, dy + dot),
              fill=(255, 255, 255, 235))
    return img


def main():
    WEB.mkdir(parents=True, exist_ok=True)
    big = make_icon(512)
    big.save(WEB / "icon.png")
    big.save(WEB / "fnplayer.ico",
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                    (64, 64), (128, 128), (256, 256)])
    (WEB / "placeholder.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="420" '
        'viewBox="0 0 300 420"><defs><linearGradient id="g" x1="0" y1="0" '
        'x2="1" y2="1"><stop offset="0" stop-color="#232837"/>'
        '<stop offset="1" stop-color="#131722"/></linearGradient></defs>'
        '<rect width="300" height="420" fill="url(#g)"/>'
        '<circle cx="150" cy="188" r="34" fill="none" stroke="#4a5268" stroke-width="3"/>'
        '<path d="M142 173 L166 188 L142 203 Z" fill="#4a5268"/>'
        '<text x="150" y="258" fill="#77809a" font-size="15" '
        'text-anchor="middle" font-family="sans-serif">暂无海报</text></svg>',
        encoding="utf-8")
    print("icon + placeholder written to", WEB)


if __name__ == "__main__":
    main()
