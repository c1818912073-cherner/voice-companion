#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从维基文库下载公版经典小说，清洗为纯文本放入 books/。

仅下载著作权已过期的公有领域作品（明代/清代小说原著）。
清洗：去模板/链接/导航，提取每回回目，压缩空行。
每回缓存到磁盘，429 限速自动退避重试，可断点续抓。

用法:
    python3 tools/fetch_classics.py            # 抓全部
    python3 tools/fetch_classics.py 紅樓夢      # 只抓指定书（维基文库名）
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_DIR = os.path.join(ROOT, "books")
CACHE_DIR = os.path.join(HERE, ".classics_cache")

API = "https://zh.wikisource.org/w/api.php"
UA = "voice-companion classics fetcher (personal local audiobook shelf)"

# 维基文库书名 → (输出文件名, 回数, 回目页名格式)
BOOKS = {
    "紅樓夢": ("红楼梦.txt", 120, "第{:03d}回"),
    "三國演義": ("三国演义.txt", 120, "第{:03d}回"),
    "西遊記": ("西游记.txt", 100, "第{:03d}回"),
    "水滸傳 (120回本)": ("水浒传.txt", 120, "第{:03d}回"),
}

_TMPL = re.compile(r"\{\{[^{}]*\}\}")
_LINK = re.compile(r"\[\[([^\]|]*)(?:\|([^\]]*))?\]\]")
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_CENTER = re.compile(r"\{\{center\|([^}]*)\}\}")
_REF = re.compile(r"<ref[^>/]*/>|<ref[^>]*>.*?</ref>", re.S | re.I)
_BR = re.compile(r"<br\s*/?>", re.I)
_TAG = re.compile(r"<[^>]+>")


def clean_chapter(wt):
    """一回的 wikitext → 纯文本（首行为回目）。"""
    wt = _COMMENT.sub("", wt)
    wt = _REF.sub("", wt)          # 校勘注释（含内容）整体去除
    wt = _BR.sub("\n", wt)
    title = ""
    m = _CENTER.search(wt)
    if m:
        title = m.group(1)
    wt = _CENTER.sub("", wt)
    # 去掉单行模板（{{樣式:古典小說}}、{{reflist}} 等）
    lines = []
    for ln in wt.split("\n"):
        s = ln.strip()
        if not s:
            lines.append("")
            continue
        if s.startswith("{{") and s.endswith("}}"):
            continue
        if s.startswith("[[../"):  # 回目导航
            continue
        if set(s) <= set("-—–"):   # 分隔线
            continue
        lines.append(ln)
    wt = "\n".join(lines)
    wt = _TMPL.sub("", wt)
    wt = _LINK.sub(lambda m: m.group(2) or m.group(1), wt)
    wt = _TAG.sub("", wt)          # poem 等剩余标签去壳，保留正文
    wt = wt.replace("'''", "").replace("''", "")
    wt = re.sub(r"\n{3,}", "\n\n", wt).strip()
    body = "\n".join(l.rstrip() for l in wt.split("\n")).strip()
    title = title.replace("'''", "").strip()
    return (title + "\n\n" if title else "") + body


def fetch(url):
    """带 429 退避的请求：10s→30s→60s。"""
    delays = (10, 30, 60)
    for attempt, delay in enumerate(delays + (None,)):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and delay is not None:
                print(f"  429 限速，{delay}s 后重试…", flush=True)
                time.sleep(delay)
                continue
            raise
        except Exception:
            if delay is None:
                raise
            time.sleep(delay)
    raise RuntimeError("unreachable")


def fetch_book(ws_name, fmt, out_path):
    cache = os.path.join(CACHE_DIR, ws_name)
    os.makedirs(cache, exist_ok=True)
    total = fmt[1]
    for n in range(1, total + 1):
        cf = os.path.join(cache, f"{n:04d}.txt")
        if os.path.exists(cf):
            continue
        page = f"{ws_name}/{fmt[2].format(n)}"
        q = urllib.parse.urlencode(
            {"action": "parse", "format": "json", "prop": "wikitext", "page": page})
        d = json.loads(fetch(f"{API}?{q}").decode("utf-8"))
        if "parse" not in d:
            raise RuntimeError(f"{page}: 页面不存在 - {d.get('error', {}).get('info')}")
        text = clean_chapter(d["parse"]["wikitext"]["*"])
        if not text.strip():
            raise RuntimeError(f"{page}: 清洗后为空")
        with open(cf, "w", encoding="utf-8") as f:
            f.write(text)
        done = sum(1 for _ in range(1, n + 1) if os.path.exists(os.path.join(cache, f"{_:04d}.txt")))
        print(f"  {page} ✓ ({len(text)} 字, {done}/{total})", flush=True)
        time.sleep(1.0)
    chapters = []
    for n in range(1, total + 1):
        with open(os.path.join(cache, f"{n:04d}.txt"), encoding="utf-8") as f:
            chapters.append(f.read().strip())
    full = "\n\n".join(chapters) + "\n"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(full)
    print(f"→ {os.path.basename(out_path)} 共 {len(full)} 字", flush=True)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    only = set(sys.argv[1:])
    for ws_name, fmt in BOOKS.items():
        if only and ws_name not in only:
            continue
        out_path = os.path.join(OUT_DIR, fmt[0])
        if os.path.exists(out_path):
            print(f"跳过 {ws_name}（已存在）")
            continue
        print(f"《{ws_name}》下载中…", flush=True)
        fetch_book(ws_name, fmt, out_path)


if __name__ == "__main__":
    main()
