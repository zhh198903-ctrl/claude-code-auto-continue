# -*- coding: utf-8 -*-
"""Generate the paste-ready WeChat article from the source article.

The source (公众号推广文章_微信版.html) references its images as local files,
which the WeChat editor cannot fetch — a plain copy-paste silently drops
every image. This inlines each <img src="..."> as a base64 data URI, so
Ctrl+A / Ctrl+C from a browser carries the images along and the editor
uploads them to its own CDN on paste.

Regenerate after ANY edit to the source article:
    python _make_paste_article.py
"""
import base64
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
MARKETING = os.path.join(HERE, "docs", "marketing")
SRC = os.path.join(MARKETING, "公众号推广文章_微信版.html")
OUT = os.path.join(MARKETING, "公众号推广文章_粘贴版.html")

MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif"}

s = open(SRC, encoding="utf-8").read()

missing = []


def inline(m):
    name = m.group(1)
    path = os.path.join(MARKETING, name)
    if not os.path.exists(path):
        missing.append(name)
        return m.group(0)
    ext = os.path.splitext(name)[1].lower()
    data = base64.b64encode(open(path, "rb").read()).decode("ascii")
    print(f"  embedded {name}  ({os.path.getsize(path)/1024:.0f} KB)")
    return f'src="data:{MIME[ext]};base64,{data}"'


s = re.sub(r'src="([^"]+\.(?:png|jpe?g|gif))"', inline, s)

if missing:
    print("ERROR missing images:", missing)
    sys.exit(1)

# The source's operating instructions describe manual image insertion, which
# no longer applies here — replace with the one-step instruction.
s = re.sub(
    r"<!-- ══ 操作提示（发布前删除此行） ══.*?══+ -->",
    "<!-- 粘贴版：图片已全部内嵌。浏览器打开 → Ctrl+A 全选 → Ctrl+C 复制 →\n"
    "     粘贴到微信公众号编辑器即可，无需手动插图。本注释不会被粘贴进去。 -->",
    s, flags=re.S)

open(OUT, "w", encoding="utf-8").write(s)
print(f"\nOUT: {OUT}  ({os.path.getsize(OUT)/1024:.0f} KB)")
