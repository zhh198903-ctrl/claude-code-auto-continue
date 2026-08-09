# -*- coding: utf-8 -*-
"""Rebuild the paste-ready WeChat article in place.

The article references its images as local files, which the WeChat editor
cannot fetch — a plain copy-paste silently drops every image. This reads
公众号推广文章_粘贴版.html, re-inlines every image from docs/marketing as a
base64 data URI, and writes it back, so Ctrl+A / Ctrl+C from a browser
carries the images along and the editor uploads them to its own CDN.

Run after replacing any screenshot:
    python _make_paste_article.py

Editing the TEXT means editing the paste version directly — it is the only
copy. Its <img> tags keep their filenames in a data-src attribute so this
script can find them again after inlining.
"""
import base64
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
MARKETING = os.path.join(HERE, "docs", "marketing")
ARTICLE = os.path.join(MARKETING, "公众号推广文章_粘贴版.html")

MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif"}

s = open(ARTICLE, encoding="utf-8").read()

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
    return f'data-src="{name}" src="data:{MIME[ext]};base64,{data}"'


# Previously inlined tags first — they carry the filename in data-src, and
# rewriting them wholesale is what makes a re-run replace rather than nest.
s = re.sub(r'data-src="([^"]+)"\s+src="data:[^"]*"', inline, s)
# Then any plain filename src (first run over a fresh article). The lookbehind
# is load-bearing: without it this also matches the `src="…"` INSIDE a
# `data-src="…"` attribute, and each run wraps the file in another layer.
s = re.sub(r'(?<![-\w])src="([^"]+\.(?:png|jpe?g|gif))"', inline, s)

if missing:
    print("ERROR missing images:", missing)
    sys.exit(1)

open(ARTICLE, "w", encoding="utf-8").write(s)
print(f"\nOUT: {ARTICLE}  ({os.path.getsize(ARTICLE)/1024:.0f} KB)")
