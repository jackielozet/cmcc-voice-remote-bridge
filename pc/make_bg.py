# -*- coding: utf-8 -*-
"""遥控器照片 -> 白底背景图 bg.png（裁掉两侧白边，程序运行时不依赖 Pillow）

窗口尺寸 / 裁剪范围必须与 remote_ui.py 里的常量一致。
"""
import os
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "assets", "remote.jpg")
DST = os.path.join(HERE, "assets", "bg.png")

# ---- 和 remote_ui.py 保持一致 ----
Z = 1.42
zp = lambda x: int(round(x * Z))
CARD_W = zp(172)
PAD = zp(8)
WIN_H = zp(800 * 0.98 + 16)
IMAGE_H = WIN_H - PAD * 2
CROP_X0, CROP_X1 = 280, 520          # 遥控器本体两侧白边裁掉
IMG_W = int(round((CROP_X1 - CROP_X0) * IMAGE_H / 800.0))
WIN_W = CARD_W * 2 + zp(16) * 2 + IMG_W


def main():
    im = Image.open(SRC).convert("RGB")
    im = im.crop((CROP_X0, 0, CROP_X1, im.size[1]))
    im = im.resize((IMG_W, IMAGE_H), Image.LANCZOS)

    bg = Image.new("RGB", (WIN_W, WIN_H), (255, 255, 255))
    bg.paste(im, ((WIN_W - IMG_W) // 2, PAD))

    bg.save(DST, optimize=True)
    print("窗口 %dx%d  图片 %dx%d  PAD=%d" % (WIN_W, WIN_H, IMG_W, IMAGE_H, PAD))
    print("输出", DST)


if __name__ == "__main__":
    main()
