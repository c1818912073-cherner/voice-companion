#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
语音伴侣 · Web 服务器 v2

功能: 音色库管理 / 分角色朗读 / 对话 / 听书进度 / 有声书导出。
仅标准库 + ffmpeg（浏览器录音转换）。复用 voice_companion 与 novel_engine。

    python3 web_server.py            # http://127.0.0.1:8890
    python3 web_server.py 8891       # 自定义端口
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import voice_companion as vc   # noqa: E402
import novel_engine as ne      # noqa: E402

WEB_DIR = os.path.join(HERE, "web")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8890
VOICES_FILE = os.path.join(HERE, "voices.json")

EXT_BY_TYPE = {
    "audio/webm": ".webm", "video/webm": ".webm",
    "audio/mp4": ".m4a", "audio/mpeg": ".mp3", "audio/ogg": ".ogg",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/aac": ".aac",
    "audio/flac": ".flac",
}

_chat_lock = threading.Lock()
_locks = {}
_export_jobs = {}   # path -> job dict


def _lock(key):
    if key not in _locks:
        _locks[key] = threading.Lock()
    return _locks[key]


# ================================================================ 音色库

SEED_VOICES = [
    {"id": "gentle_f", "name": "温柔女声",
     "instructions": "A gentle, warm young woman's voice, soft and calm, natural conversational pace", "speed": 1.0},
    {"id": "calm_m", "name": "沉稳男声",
     "instructions": "A calm, deep male voice with steady, unhurried pace, like a caring friend", "speed": 1.0},
    {"id": "lively_g", "name": "活泼少女",
     "instructions": "A cheerful young female voice with high pitch, energetic and fast", "speed": 1.05},
    {"id": "storyteller", "name": "说书先生",
     "instructions": "A dramatic male storyteller voice, rich and expressive, like a traditional Chinese pingshu performer", "speed": 0.95},
    {"id": "child", "name": "稚气童声",
     "instructions": "A cute child's voice, playful, lively and curious", "speed": 1.0},
    {"id": "anchor", "name": "新闻播音",
     "instructions": "A professional news anchor voice, neutral tone, clear and precise articulation", "speed": 1.0},
    {"id": "radio", "name": "深夜电台",
     "instructions": "A soft, soothing late-night radio host voice, low volume, slow and intimate", "speed": 0.9},
    {"id": "british", "name": "英伦绅士",
     "instructions": "A refined British gentleman's voice, elegant, measured, with warm timbre", "speed": 1.0},
]


def load_library():
    lib = {"current": "gentle_f", "voices": list(SEED_VOICES)}
    if os.path.exists(VOICES_FILE):
        try:
            with open(VOICES_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            ids = {v["id"] for v in saved.get("voices", [])}
            merged = [v for v in SEED_VOICES if v["id"] not in ids]
            lib["voices"] = saved.get("voices", []) + merged
            lib["current"] = saved.get("current") or lib["current"]
        except Exception:
            pass
    if not any(v["id"] == lib["current"] for v in lib["voices"]):
        lib["current"] = lib["voices"][0]["id"]
    return lib


def save_library(lib):
    tmp = VOICES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, indent=1)
    os.replace(tmp, VOICES_FILE)
    # 同步到终端版当前音色
    v = voice_by_id(lib["current"])
    if v:
        vc.CFG["voice"] = {"name": v["name"], "instructions": v["instructions"],
                           "speed": v.get("speed", 1.0)}
        cfg_path = os.path.join(HERE, "config.json")
        disk = {}
        if os.path.exists(cfg_path):
            with open(cfg_path, encoding="utf-8") as f:
                try:
                    disk = json.load(f)
                except Exception:
                    disk = {}
        disk["voice"] = vc.CFG["voice"]
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(disk, f, ensure_ascii=False, indent=2)


def voice_by_id(vid):
    if not vid:
        return None
    for v in load_library()["voices"]:
        if v["id"] == vid:
            return v
    return None


def current_voice():
    lib = load_library()
    return voice_by_id(lib["current"]) or lib["voices"][0]


def voice_sig(v):
    return hashlib.md5(
        (v["instructions"] + "|" + str(v.get("speed", 1.0))).encode("utf-8")
    ).hexdigest()[:10]


# ================================================================ 书籍/脚本/选角


def safe_book_path(rel):
    base = os.path.abspath(vc.BOOKS_DIR)
    p = os.path.abspath(os.path.join(base, rel))
    if not p.startswith(base + os.sep):
        raise ValueError("非法路径")
    return p


def book_cache_base(book_path):
    h = hashlib.md5(os.path.abspath(book_path).encode("utf-8")).hexdigest()[:12]
    d = os.path.join(vc.BOOKS_DIR, ".cache", h)
    os.makedirs(d, exist_ok=True)
    return d


def load_script(book_path, deep=False):
    """规则切分(快,打开书即做) + LLM 人物识别(deep=True 时,一次)。
    结果缓存于 .cache/<hash>/，书籍文件改动(mtime 变化)自动失效。"""
    base = book_cache_base(book_path)
    script_f = os.path.join(base, "script.json")
    chars_f = os.path.join(base, "characters.json")
    mtime = os.path.getmtime(book_path)
    text = None

    with _lock("script:" + base):
        lines = None
        if os.path.exists(script_f):
            try:
                sd = json.load(open(script_f, encoding="utf-8"))
                if sd.get("mtime") == mtime:
                    lines = sd["lines"]
            except Exception:
                lines = None
        if lines is None:
            text = vc.extract_text(book_path)
            lines = ne.extract_script(text)
            with open(script_f, "w", encoding="utf-8") as f:
                json.dump({"mtime": mtime, "lines": lines}, f, ensure_ascii=False)

        characters = None
        if os.path.exists(chars_f):
            try:
                cd = json.load(open(chars_f, encoding="utf-8"))
                if cd.get("mtime") == mtime:
                    characters = cd.get("characters") or []
            except Exception:
                characters = None
        if deep and not characters:
            if text is None:
                text = vc.extract_text(book_path)
            characters = ne.build_characters(text, lines)
            with open(chars_f, "w", encoding="utf-8") as f:
                json.dump({"mtime": mtime, "characters": characters}, f, ensure_ascii=False)
        if characters is None:
            characters = []
    return lines, characters


def load_cast(book_path):
    base = book_cache_base(book_path)
    cast_f = os.path.join(base, "cast.json")
    if os.path.exists(cast_f):
        try:
            return json.load(open(cast_f, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cast(book_path, cast):
    base = book_cache_base(book_path)
    with open(os.path.join(base, "cast.json"), "w", encoding="utf-8") as f:
        json.dump(cast, f, ensure_ascii=False, indent=1)


def default_cast(characters):
    """按性别自动分配：旁白=当前音色, 男=沉稳男声, 女=温柔女声。"""
    cast = {"旁白": current_voice()["id"]}
    for c in characters:
        g = c.get("gender")
        if c["name"] == "旁白":
            continue
        cast[c["name"]] = {"male": "calm_m", "female": "gentle_f"}.get(g, current_voice()["id"])
    return cast


def voice_for_line(book_path, speaker):
    cast = load_cast(book_path)
    v = voice_by_id(cast.get(speaker))
    return v or current_voice()


def list_books():
    prog = vc.load_progress()
    out = []
    for f in sorted(os.listdir(vc.BOOKS_DIR)):
        p = os.path.join(vc.BOOKS_DIR, f)
        if not os.path.isfile(p) or f.startswith(".") or f == "进度.json":
            continue
        pr = prog.get(os.path.abspath(p), {})
        out.append({
            "name": f,
            "idx": pr.get("idx", 0),
            "total": pr.get("total", 0),
            "updated": pr.get("updated", ""),
        })
    return out


# ================================================================ 有声书导出


def export_worker(book_path, job):
    try:
        lines, _ = load_script(book_path)
        job["total"] = len(lines)
        wav_paths = []
        for i, ln in enumerate(lines):
            v = voice_for_line(book_path, ln["s"])
            d = os.path.join(book_cache_base(book_path), "v-" + voice_sig(v))
            os.makedirs(d, exist_ok=True)
            wav = os.path.join(d, f"seg_{i:06d}.wav")
            if not os.path.exists(wav):
                vc.tts_to_file(ln["t"], wav, voice=v)
            wav_paths.append(wav)
            job["done"] = i + 1
        out_dir = os.path.join(vc.BOOKS_DIR, ".cache", "exports")
        os.makedirs(out_dir, exist_ok=True)
        name = os.path.splitext(os.path.basename(book_path))[0]
        out = os.path.join(out_dir, f"{name}.mp3")
        lst = os.path.join(out_dir, "list.txt")
        with open(lst, "w", encoding="utf-8") as f:
            for w in wav_paths:
                f.write(f"file '{w}'\n")
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst,
             "-b:a", "96k", out],
            capture_output=True, timeout=600,
        )
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode("utf-8", "replace")[-300:])
        job["state"] = "done"
        job["file"] = out
    except Exception as e:
        job["state"] = "error"
        job["error"] = str(e)


# ================================================================ 录音转换


def ffmpeg_to_wav(data, hint_type):
    ext = EXT_BY_TYPE.get((hint_type or "").split(";")[0].strip(), ".webm")
    tmp = tempfile.mkdtemp(prefix="vc_web_")
    try:
        src = os.path.join(tmp, "in" + ext)
        dst = os.path.join(tmp, "out.wav")
        with open(src, "wb") as f:
            f.write(data)
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-ar", "16000", "-ac", "1", dst],
            capture_output=True, timeout=60,
        )
        if r.returncode != 0 or not os.path.exists(dst):
            raise RuntimeError("音频转换失败: " + r.stderr.decode("utf-8", "replace")[-200:])
        with open(dst, "rb") as f:
            return f.read()
    finally:
        for f in os.listdir(tmp):
            try:
                os.remove(os.path.join(tmp, f))
            except OSError:
                pass
        os.rmdir(tmp)


# ================================================================ HTTP


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("VC_WEB_VERBOSE"):
            super().log_message(fmt, *args)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _bytes(self, data, ctype, download_name=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if download_name:
            from urllib.parse import quote
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{quote(download_name)}")
        self.end_headers()
        self.wfile.write(data)

    def _wav(self, data):
        self._bytes(data, "audio/wav")

    def _err(self, msg, code=400):
        self._json({"error": str(msg)}, code)

    # ---------------------------------------------------- GET

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/":
                with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                    self._bytes(f.read(), "text/html; charset=utf-8")
            elif u.path == "/api/state":
                lib = load_library()
                self._json({
                    "voice": current_voice(),
                    "currentId": lib["current"],
                    "voices": lib["voices"],
                    "books": list_books(),
                    "models": {
                        "asr": vc.CFG["asr_model"],
                        "tts": vc.CFG["tts_model"],
                        "chat": vc.CFG["chat_model"],
                    },
                })
            elif u.path == "/api/book":
                p = safe_book_path(qstr(q, "path"))
                lines, characters = load_script(p)
                cast = load_cast(p)
                if characters and not cast:
                    cast = default_cast(characters)
                    save_cast(p, cast)
                prog = vc.load_progress().get(os.path.abspath(p), {})
                self._json({
                    "name": os.path.basename(p),
                    "lines": lines,
                    "characters": characters,
                    "cast": cast,
                    "progress": prog.get("idx", 0),
                })
            elif u.path == "/api/export/status":
                p = safe_book_path(qstr(q, "path"))
                job = _export_jobs.get(os.path.abspath(p))
                if not job:
                    return self._err("没有进行中的导出任务", 404)
                self._json({k: job.get(k) for k in ("state", "done", "total", "error")})
            elif u.path == "/api/export/file":
                p = safe_book_path(qstr(q, "path"))
                job = _export_jobs.get(os.path.abspath(p))
                if not job or job.get("state") != "done":
                    return self._err("导出未完成", 404)
                with open(job["file"], "rb") as f:
                    self._bytes(f.read(), "audio/mpeg",
                                download_name=os.path.basename(job["file"]))
            else:
                # 静态文件（仅 web/ 目录下的白名单类型，防目录穿越）
                _static_types = {".svg": "image/svg+xml", ".png": "image/png",
                                 ".ico": "image/x-icon", ".css": "text/css",
                                 ".js": "text/javascript"}
                name = os.path.basename(u.path)
                ext = os.path.splitext(name)[1].lower()
                p = os.path.join(WEB_DIR, name)
                if name and ext in _static_types and os.path.isfile(p):
                    with open(p, "rb") as f:
                        self._bytes(f.read(), _static_types[ext])
                else:
                    self._err("not found", 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            self._err(e, 500)

    # ---------------------------------------------------- POST

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/api/voices/save":
                d = json.loads(self._body())
                name = str(d.get("name") or "").strip()[:30]
                instructions = str(d.get("instructions") or "").strip()
                if not name or not instructions:
                    return self._err("名称和描述不能为空")
                lib = load_library()
                vid = str(d.get("id") or "").strip()
                entry = {
                    "id": vid or "v" + uuid.uuid4().hex[:8],
                    "name": name,
                    "instructions": instructions,
                    "speed": min(1.5, max(0.5, float(d.get("speed") or 1.0))),
                }
                ids = [v["id"] for v in lib["voices"]]
                if entry["id"] in ids:
                    lib["voices"] = [entry if v["id"] == entry["id"] else v
                                     for v in lib["voices"]]
                else:
                    lib["voices"].append(entry)
                if d.get("makeCurrent"):
                    lib["current"] = entry["id"]
                save_library(lib)
                self._json({"ok": True, "voice": entry, "currentId": lib["current"]})

            elif u.path == "/api/voices/delete":
                d = json.loads(self._body())
                lib = load_library()
                vid = d.get("id")
                if any(v["id"] == vid and v["id"] in {s["id"] for s in SEED_VOICES}
                       for v in lib["voices"]):
                    return self._err("预置音色不可删除，可以编辑覆盖")
                lib["voices"] = [v for v in lib["voices"] if v["id"] != vid]
                if lib["current"] == vid and lib["voices"]:
                    lib["current"] = lib["voices"][0]["id"]
                save_library(lib)
                self._json({"ok": True})

            elif u.path == "/api/voices/current":
                d = json.loads(self._body())
                lib = load_library()
                if not voice_by_id(d.get("id")):
                    return self._err("音色不存在")
                lib["current"] = d["id"]
                save_library(lib)
                self._json({"ok": True, "voice": current_voice()})

            elif u.path == "/api/tts":
                d = json.loads(self._body())
                text = str(d.get("text") or "").strip()
                if not text:
                    return self._err("text 为空")
                voice = None
                if d.get("voiceId"):
                    voice = voice_by_id(d["voiceId"])
                elif isinstance(d.get("voice"), dict) and d["voice"].get("instructions"):
                    voice = {"instructions": d["voice"]["instructions"],
                             "speed": d["voice"].get("speed", 1.0)}
                self._wav(vc.tts_bytes(text, voice=voice))

            elif u.path == "/api/asr":
                data = self._body()
                if not data:
                    return self._err("空音频")
                wav = ffmpeg_to_wav(data, self.headers.get("X-Audio-Type", ""))
                tmp = tempfile.mktemp(suffix=".wav")
                try:
                    with open(tmp, "wb") as f:
                        f.write(wav)
                    self._json({"text": vc.transcribe(tmp)})
                finally:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

            elif u.path == "/api/chat":
                d = json.loads(self._body())
                if d.get("reset"):
                    with _chat_lock:
                        vc._history.clear()
                    return self._json({"reply": "", "reset": True})
                text = str(d.get("text") or "").strip()
                if not text:
                    return self._err("text 为空")
                self._json({"reply": vc.think(text)})

            elif u.path == "/api/script":
                # 把一段文本切成"说话人+台词"（用于对话回复的分角色朗读）
                d = json.loads(self._body())
                text = str(d.get("text") or "").strip()
                if not text:
                    return self._err("text 为空")
                self._json({"lines": ne.extract_script(text)})

            elif u.path == "/api/book/analyze":
                d = json.loads(self._body())
                p = safe_book_path(d["path"])
                lines, characters = load_script(p, deep=True)
                cast = load_cast(p)
                if not cast or d.get("rebuild"):
                    cast = default_cast(characters)
                    save_cast(p, cast)
                self._json({"lines": lines, "characters": characters, "cast": cast})

            elif u.path == "/api/book/cast":
                d = json.loads(self._body())
                p = safe_book_path(d["path"])
                cast = {str(k): str(v) for k, v in (d.get("cast") or {}).items()}
                save_cast(p, cast)
                self._json({"ok": True})

            elif u.path == "/api/segment":
                d = json.loads(self._body())
                p = safe_book_path(d["path"])
                idx = int(d["idx"])
                lines, _ = load_script(p)
                if not (0 <= idx < len(lines)):
                    return self._err("段号越界", 404)
                v = voice_for_line(p, lines[idx]["s"])
                dcache = os.path.join(book_cache_base(p), "v-" + voice_sig(v))
                os.makedirs(dcache, exist_ok=True)
                wav = os.path.join(dcache, f"seg_{idx:06d}.wav")
                with _lock(f"seg:{os.path.abspath(p)}"):
                    if not os.path.exists(wav):
                        vc.tts_to_file(lines[idx]["t"], wav, voice=v)
                with open(wav, "rb") as f:
                    self._wav(f.read())

            elif u.path == "/api/progress":
                d = json.loads(self._body())
                p = safe_book_path(d["path"])
                idx = max(0, int(d["idx"]))
                lines, _ = load_script(p)
                vc.save_progress(p, idx if idx < len(lines) else 0, len(lines))
                self._json({"ok": True})

            elif u.path == "/api/books/upload":
                name = qstr(q, "name", "book.txt")
                name = os.path.basename(name).strip() or "book.txt"
                data = self._body()
                if not data:
                    return self._err("空文件")
                os.makedirs(vc.BOOKS_DIR, exist_ok=True)
                with open(safe_book_path(name), "wb") as f:
                    f.write(data)
                self._json({"ok": True, "name": name})

            elif u.path == "/api/books/delete":
                d = json.loads(self._body())
                p = safe_book_path(d["path"])
                if os.path.exists(p):
                    os.remove(p)
                h = hashlib.md5(os.path.abspath(p).encode("utf-8")).hexdigest()[:12]
                shutil.rmtree(os.path.join(vc.BOOKS_DIR, ".cache", h), ignore_errors=True)
                prog = vc.load_progress()
                prog.pop(os.path.abspath(p), None)
                tmp = vc.PROGRESS_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(prog, f, ensure_ascii=False, indent=1)
                os.replace(tmp, vc.PROGRESS_FILE)
                self._json({"ok": True})

            elif u.path == "/api/export":
                d = json.loads(self._body())
                p = safe_book_path(d["path"])
                key = os.path.abspath(p)
                job = _export_jobs.get(key)
                if job and job.get("state") == "running":
                    return self._json({"ok": True, "state": "running"})
                job = {"state": "running", "done": 0, "total": 0, "error": None}
                _export_jobs[key] = job
                threading.Thread(target=export_worker, args=(p, job), daemon=True).start()
                self._json({"ok": True, "state": "running"})

            else:
                self._err("not found", 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            self._err(e, 500)


def qstr(q, key, default=""):
    """取查询参数并兼容未转义的原始 UTF-8 字节。"""
    v = (q.get(key) or [default])[0]
    try:
        return v.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return v


def main():
    os.makedirs(vc.BOOKS_DIR, exist_ok=True)
    load_library()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"语音伴侣 Web v2: http://127.0.0.1:{PORT}  (Ctrl+C 停止)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
