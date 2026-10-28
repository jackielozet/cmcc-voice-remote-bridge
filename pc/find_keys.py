# -*- coding: utf-8 -*-
"""从遥控器照片里自动找出每个键的中心坐标（连通域分析，纯 Python）"""
import os
import sys
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "assets", "remote.jpg")

# 键是纯黑的圆/椭圆，遥控器本体是深灰，靠亮度分开
THRESH = 45
MIN_AREA = 260
MAX_AREA = 9000


def main():
    im = Image.open(SRC).convert("RGB")
    w, h = im.size
    px = im.load()

    lum = [[0] * w for _ in range(h)]
    for y in range(h):
        row = lum[y]
        for x in range(w):
            r, g, b = px[x, y]
            row[x] = (r * 299 + g * 587 + b * 114) // 1000

    seen = [[False] * w for _ in range(h)]
    blobs = []
    for y0 in range(h):
        for x0 in range(w):
            if seen[y0][x0] or lum[y0][x0] > THRESH:
                continue
            # BFS
            stack = [(x0, y0)]
            seen[y0][x0] = True
            pts = []
            while stack:
                x, y = stack.pop()
                pts.append((x, y))
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < w and 0 <= ny < h and not seen[ny][nx] and lum[ny][nx] <= THRESH:
                        seen[ny][nx] = True
                        stack.append((nx, ny))
            if not (MIN_AREA <= len(pts) <= MAX_AREA):
                continue
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            bw, bh = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
            fill = len(pts) / float(bw * bh)
            # 键是近似方形/圆形，填充率较高；细长的是边框或图标
            if fill < 0.45 or bw < 14 or bh < 14:
                continue
            blobs.append(dict(cx=sum(xs) // len(xs), cy=sum(ys) // len(ys),
                              w=bw, h=bh, area=len(pts), fill=round(fill, 2)))

    blobs.sort(key=lambda b: (b["cy"], b["cx"]))
    print("找到 %d 个候选键" % len(blobs))
    for b in blobs:
        print("  x=%3d y=%3d  %2dx%-2d  面积=%-5d 填充=%.2f" %
              (b["cx"], b["cy"], b["w"], b["h"], b["area"], b["fill"]))


if __name__ == "__main__":
    main()
