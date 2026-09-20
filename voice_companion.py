#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
语音伴侣 · 朗读 + 对话 + 自定义音色（omlx 本地最小模型，全程离线）

模型（本机 omlx 上同类最小的三个）:
    识别  Qwen3-ASR-1.7B-bf16                  (1.7B)
    合成  Qwen3-TTS-12Hz-1.7B-VoiceDesign-bf16 (1.7B, 音色可描述)
    对话  Qwythos-9B-Claude-Mythos-5-1M-mxfp4  (9B, 最小的 LLM)

用法:
    python3 voice_companion.py            # 菜单
    python3 voice_companion.py --selftest # 全链路自检（含朗读管线）
"""

import json
import os
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
BOOKS_DIR = os.path.join(HERE, "books")
PROGRESS_FILE = os.path.join(BOOKS_DIR, "进度.json")

# ---------------------------------------------------------------- 默认配置

DEFAULTS = {
    "base_url": "http://127.0.0.1:8880/v1",
    "asr_model": "Qwen3-ASR-1.7B-bf16",
    "tts_model": "Qwen3-TTS-12Hz-1.7B-VoiceDesign-bf16",
    "chat_model": "Qwythos-9B-Claude-Mythos-5-1M-mxfp4-mlx",
    "voice": {
        "name": "温柔女声",
        "instructions": "A gentle, warm young woman's voice, soft and calm, natural conversational pace",
        "speed": 1.0,
    },
    "system_prompt": (
        "你是用户的语音伴侣，名叫小伴，性格温暖、有点俏皮。"
        "你的回复会被直接朗读出来，所以必须口语化、自然，每次不超过三句话，"
        "不要用任何符号列表、代码、英文单词（除非必要），不要输出 emoji。"
    ),
    "max_history": 20,
    "max_reply_chars": 200,
    "segment_max_chars": 280,      # 朗读书籍时每段最大字符数
    "prefetch_segments": 3,        # 朗读时预合成的段数
    "silence_threshold_pct": 2.5,  # 对话录音: 触发音量阈值
    "silence_stop_seconds": 1.4,   # 对话录音: 静音多久算说完
    "record_max_seconds": 30,
    "asr_language": "zh",
}

# 预置音色（英文描述为官方推荐写法；中文描述同样支持）
VOICE_PRESETS = {
    "1": ("温柔女声", "A gentle, warm young woman's voice, soft and calm, natural conversational pace"),
    "2": ("沉稳男声", "A calm, deep male voice with steady, unhurried pace, like a caring friend"),
    "3": ("活泼少女", "A cheerful young female voice with high pitch, energetic and fast"),
    "4": ("说书先生", "A dramatic male storyteller voice, rich and expressive, like a traditional Chinese pingshu performer"),
    "5": ("稚气童声", "A cute child's voice, playful, lively and curious"),
    "6": ("新闻播音", "A professional news anchor voice, neutral tone, clear and precise articulation"),
    "7": ("深夜电台", "A soft, soothing late-night radio host voice, low volume, slow and intimate"),
    "8": ("英伦绅士", "A refined British gentleman's voice, elegant, measured, with warm timbre"),
}


def load_config():
    cfg = dict(DEFAULTS)
    path = os.path.join(HERE, "config.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
        cfg.update({k: v for k, v in saved.items() if k != "voice" or isinstance(v, dict)})
        if isinstance(saved.get("voice"), dict):
            cfg["voice"] = {**DEFAULTS["voice"], **saved["voice"]}
    if not cfg.get("api_key"):
        try:
            with open(os.path.expanduser("~/.omlx/settings.json"), encoding="utf-8") as f:
                cfg["api_key"] = json.load(f)["auth"]["api_key"]
        except Exception:
            cfg["api_key"] = ""
    return cfg


CFG = load_config()

# ---------------------------------------------------------------- HTTP


def _request(endpoint, data, headers, timeout):
    req = urllib.request.Request(CFG["base_url"] + endpoint, data=data,
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _json_headers():
    return {
        "Authorization": "Bearer " + CFG["api_key"],
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------- TTS / ASR / LLM


def tts_bytes(text, voice=None, speed=None):
    """合成一段文字，返回 wav 字节流。voice 传 {"instructions":…, "speed":…} 可临时覆盖。"""
    v = voice or CFG["voice"]
    instructions = v["instructions"]
    sp = speed if speed is not None else v.get("speed", 1.0)
    payload = {
        "model": CFG["tts_model"],
        "input": text,
        "instructions": instructions,
        "speed": sp,
        "response_format": "wav",
    }
    raw = _request("/audio/speech", json.dumps(payload).encode("utf-8"),
                   _json_headers(), timeout=300)
    if raw[:4] != b"RIFF":
        raise RuntimeError("TTS 返回异常: " + raw[:200].decode("utf-8", "replace"))
    return raw


def tts_to_file(text, wav_path, voice=None, speed=None):
    """合成一段文字到 wav 文件。"""
    with open(wav_path, "wb") as f:
        f.write(tts_bytes(text, voice=voice, speed=speed))
    return wav_path


def speak(text, voice=None):
    """合成并同步播放一句话（对话模式用）。"""
    tmp = tempfile.mkdtemp(prefix="vc_")
    try:
        wav = os.path.join(tmp, "s.wav")
        tts_to_file(text, wav, voice=voice)
        subprocess.run(["afplay", wav], check=False)
    finally:
        try:
            os.remove(os.path.join(tmp, "s.wav"))
            os.rmdir(tmp)
        except OSError:
            pass


def transcribe(path):
    boundary = "----vcompanion" + uuid.uuid4().hex
    with open(path, "rb") as f:
        audio = f.read()
    parts = []
    for name, value in (("model", CFG["asr_model"]),
                        ("language", CFG["asr_language"]),
                        ("response_format", "json")):
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="u.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode()
    )
    parts.append(audio)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        CFG["base_url"] + "/audio/transcriptions", data=b"".join(parts),
        headers={
            "Authorization": "Bearer " + CFG["api_key"],
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("text") or "").strip()


_history = []


def think(user_text, system=None, history=None, max_tokens=None, max_chars=None,
          keep_lines=False):
    """对话生成。system/history/max_tokens/max_chars 可覆盖（剧情模式等专用场景），
    默认走闲聊配置与全局记忆；传入 history 时不读写全局记忆。
    keep_lines=True 时保留换行（结构化输出用），否则压成单行。"""
    if history is None:
        _history.append({"role": "user", "content": user_text})
        history = _history[-CFG["max_history"]:]
        own = True
    else:  # 外部历史：本轮输入追加在末尾，不读写全局记忆
        history = list(history) + [{"role": "user", "content": user_text}]
        own = False
    messages = [{"role": "system", "content": system or CFG["system_prompt"]}] + history
    payload = {
        "model": CFG["chat_model"],
        "messages": messages,
        "max_tokens": max_tokens or 280,
        "temperature": 0.7,
        "chat_template_kwargs": {"enable_thinking": False},
        "tool_choice": "none",
    }
    raw = _request("/chat/completions", json.dumps(payload).encode("utf-8"),
                   _json_headers(), timeout=300)
    msg = json.loads(raw.decode("utf-8"))["choices"][0]["message"]
    reply = (msg.get("content") or "").strip()
    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.S).strip()
    reply = re.sub(r"[*#`_~>]+", "", reply)
    reply = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]", "", reply)
    if keep_lines:
        reply = re.sub(r"[ \t]+", " ", reply).strip()
    else:
        reply = re.sub(r"\s+", " ", reply).strip()
    limit = max_chars or CFG["max_reply_chars"]
    if len(reply) > limit:
        reply = reply[:limit] + "……先说这些。"
    if own:
        _history.append({"role": "assistant", "content": reply})
    return reply


# ---------------------------------------------------------------- 对话模式


def record_utterance(path):
    cmd = [
        "rec", "-q", "-r", "16000", "-c", "1", "-b", "16", "-e", "signed-integer",
        path,
        "trim", "0", str(CFG["record_max_seconds"]),
        "silence", "1", "0.08", f"{CFG['silence_threshold_pct']}%",
        "1", str(CFG["silence_stop_seconds"]), f"{CFG['silence_threshold_pct']}%",
    ]
    try:
        subprocess.run(cmd, check=True, timeout=CFG["record_max_seconds"] + 15)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass
    try:
        return os.path.getsize(path) > 8000
    except OSError:
        return False


def chat_mode():
    print("\n── 对话模式 ─────────────────────────────")
    print(" 直接说话即可（静音 1.4 秒自动断句）。")
    print(" 说「再见 / 拜拜」或 Ctrl+C 返回菜单。首次使用需授权麦克风。\n")
    wav = os.path.join(tempfile.gettempdir(), "vc_utterance.wav")
    try:
        speak("你好，我是小伴，想聊点什么？")
        while True:
            if not record_utterance(wav):
                continue
            text = transcribe(wav)
            if not text:
                continue
            print(f"  你: {text}")
            if any(w in text for w in ("再见", "拜拜", "返回菜单")):
                speak("好的，随时来找我聊。")
                return
            t0 = time.time()
            reply = think(text)
            print(f"  小伴: {reply}   [{time.time()-t0:.1f}s]")
            speak(reply)
    except KeyboardInterrupt:
        print()
        return


# ---------------------------------------------------------------- 朗读模式


def extract_text(path):
    """把 txt/md/pdf/doc(x)/rtf/html 统一抽成纯文本。"""
    ext = os.path.splitext(path)[1].lower()
    tmpdir = tempfile.mkdtemp(prefix="vc_book_")
    try:
        if ext in (".txt", ".md", ".markdown", ""):
            raw = open(path, "rb").read()
        elif ext == ".pdf":
            out = os.path.join(tmpdir, "out.txt")
            subprocess.run(["pdftotext", "-enc", "UTF-8", path, out],
                           check=True, capture_output=True, timeout=120)
            raw = open(out, "rb").read()
        elif ext in (".doc", ".docx", ".rtf", ".html", ".htm", ".webarchive"):
            out = os.path.join(tmpdir, "out.txt")
            subprocess.run(["textutil", "-convert", "txt", "-encoding", "UTF-8",
                            "-output", out, path],
                           check=True, capture_output=True, timeout=120)
            raw = open(out, "rb").read()
        else:
            raise ValueError(f"暂不支持的格式: {ext}（支持 txt/md/pdf/doc/docx/rtf/html）")
        for enc in ("utf-8", "gb18030", "big5"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")
    finally:
        for f in os.listdir(tmpdir):
            try:
                os.remove(os.path.join(tmpdir, f))
            except OSError:
                pass
        os.rmdir(tmpdir)


def split_segments(text, max_chars):
    """按段落切分，长段再按句号等切分，保持每段不超过 max_chars。"""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    segments = []
    for para in paragraphs:
        para = re.sub(r"\s+", " ", para)
        if len(para) <= max_chars:
            segments.append(para)
            continue
        buf = ""
        for sent in re.split(r"(?<=[。！？；!?;])", para):
            if len(buf) + len(sent) > max_chars and buf:
                segments.append(buf)
                buf = sent
            else:
                buf += sent
        if buf:
            segments.append(buf)
    return segments


def load_progress():
    try:
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_progress(book_path, idx, total):
    os.makedirs(BOOKS_DIR, exist_ok=True)
    prog = load_progress()
    prog[os.path.abspath(book_path)] = {"idx": idx, "total": total,
                                        "updated": time.strftime("%Y-%m-%d %H:%M")}
    tmp = PROGRESS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(prog, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PROGRESS_FILE)


def _key_listener(stop_flag, commands):
    """终端原始模式监听按键: 空格=暂停 n=下一段 b=上一段 q=退出。"""
    import termios
    import tty
    try:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
    except (termios.error, ValueError, OSError):
        return  # 无终端（如管道测试），跳过按键控制
    try:
        tty.setcbreak(fd)
        while not stop_flag.is_set():
            ch = sys.stdin.read(1)
            if ch == " ":
                commands.put("pause")
            elif ch == "n":
                commands.put("next")
            elif ch == "b":
                commands.put("prev")
            elif ch in ("q", "\x03"):
                commands.put("quit")
    except Exception:
        pass
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass


def read_book(book_path, start_idx=0):
    text = extract_text(book_path)
    segments = split_segments(text, CFG["segment_max_chars"])
    if not segments:
        print("  没有可朗读的内容。")
        return
    total = len(segments)
    name = os.path.basename(book_path)
    print(f"\n── 朗读《{name}》──────── 共 {total} 段")
    print("  控制: 空格=暂停/继续  n=下一段  b=上一段  q=退出并记进度\n")

    tmpdir = tempfile.mkdtemp(prefix="vc_read_")
    commands = queue.Queue()
    stop_flag = threading.Event()
    threading.Thread(target=_key_listener, args=(stop_flag, commands), daemon=True).start()

    # 就绪表: {idx: wav路径}；cond 同时用于"段就绪"与"可继续合成"两类等待
    cond = threading.Condition()
    ready = {}
    state = {"frontier": start_idx, "player_idx": start_idx, "error": None}

    def producer():
        i = start_idx
        while i < total and not stop_flag.is_set():
            with cond:
                while i - state["player_idx"] > CFG["prefetch_segments"] and not stop_flag.is_set():
                    cond.wait(timeout=0.5)
                if stop_flag.is_set():
                    return
            wav = os.path.join(tmpdir, f"seg_{i}.wav")
            try:
                if not os.path.exists(wav):
                    tts_to_file(segments[i], wav)
                with cond:
                    ready[i] = wav
                    state["frontier"] = i + 1
                    cond.notify_all()
                i += 1
            except Exception as e:
                state["error"] = f"合成第 {i+1} 段失败: {e}"
                stop_flag.set()
                with cond:
                    cond.notify_all()
                return

    threading.Thread(target=producer, daemon=True).start()

    def wait_segment(idx):
        """阻塞直到 idx 段就绪; 返回 wav 路径或 None(出错/终止)。"""
        with cond:
            while idx not in ready:
                if state["error"] or stop_flag.is_set():
                    return None
                if idx >= total:
                    return None
                cond.wait(timeout=0.4)
            return ready[idx]

    def get_cmd(block, timeout=0.2):
        try:
            return commands.get(timeout=timeout) if block else commands.get_nowait()
        except queue.Empty:
            return None

    def play_with_controls(idx, wav):
        """播放一段，期间响应控制键。返回 'done'|'pause'|'next'|'prev'|'quit'。"""
        proc = subprocess.Popen(["afplay", wav])
        current_proc["p"] = proc
        try:
            while proc.poll() is None:
                cmd = get_cmd(True, 0.15)
                if cmd == "quit":
                    proc.terminate(); proc.wait()
                    return "quit"
                if cmd == "pause":
                    proc.terminate(); proc.wait()
                    return "pause"
                if cmd == "next":
                    proc.terminate(); proc.wait()
                    return "next"
                if cmd == "prev":
                    proc.terminate(); proc.wait()
                    return "prev"
        finally:
            current_proc["p"] = None
        return "done"

    idx = start_idx
    paused = False
    current_proc = {"p": None}  # 退出时终止在播的 afplay
    try:
        while 0 <= idx < total:
            if state["error"]:
                print(f"\n  ⚠ {state['error']}")
                return
            wav = wait_segment(idx)
            if wav is None:
                if idx >= total:
                    break
                if stop_flag.is_set() and not state["error"]:
                    continue
                continue

            preview = segments[idx][:26] + ("…" if len(segments[idx]) > 26 else "")
            print(f"  [{idx+1}/{total}] {preview}", flush=True)

            if paused:  # 从暂停恢复时重播本段
                while paused:
                    cmd = get_cmd(True, 0.3)
                    if cmd == "pause":
                        paused = False
                    elif cmd == "quit":
                        raise KeyboardInterrupt
                    elif cmd == "next":
                        paused = False; idx += 1
                    elif cmd == "prev":
                        paused = False; idx = max(0, idx - 1)
                if not (0 <= idx < total):
                    continue
                wav = wait_segment(idx) or wav

            r = play_with_controls(idx, wav)
            if r == "quit":
                raise KeyboardInterrupt
            elif r == "pause":
                paused = True
                print("  [暂停] 按空格继续")
                continue
            elif r == "next":
                idx += 1
                continue
            elif r == "prev":
                idx = max(0, idx - 1)
                continue
            with cond:
                state["player_idx"] = idx + 1
                cond.notify_all()
            idx += 1

        print(f"\n  📖 《{name}》朗读完毕！进度已清除。")
        save_progress(book_path, 0, total)
    except KeyboardInterrupt:
        save_progress(book_path, idx, total)
        print(f"\n  🔖 已保存进度: 第 {min(idx+1, total)}/{total} 段，下次可继续。")
    finally:
        stop_flag.set()
        if current_proc["p"] is not None:
            try:
                current_proc["p"].terminate()
            except OSError:
                pass
        with cond:
            cond.notify_all()
        for f in os.listdir(tmpdir):
            try:
                os.remove(os.path.join(tmpdir, f))
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass


def reading_mode():
    os.makedirs(BOOKS_DIR, exist_ok=True)
    books = sorted(
        f for f in os.listdir(BOOKS_DIR)
        if os.path.isfile(os.path.join(BOOKS_DIR, f))
        and not f.startswith(".")
        and not f.endswith(".json")
    )
    print("\n── 朗读模式 ─────────────────────────────")
    if books:
        for i, b in enumerate(books, 1):
            mark = ""
            prog = load_progress().get(os.path.abspath(os.path.join(BOOKS_DIR, b)))
            if prog and prog.get("idx", 0) > 0:
                mark = f"  🔖 读到 {prog['idx']+1}/{prog['total']} 段"
            print(f"  {i}. {b}{mark}")
        print(f"  n. 直接输入书籍文件路径")
    else:
        print(f"  books/ 目录还是空的，把 txt/pdf/doc 书籍放进去，或直接输入路径。")
    choice = input("\n  选择编号或路径（回车返回）: ").strip()
    if not choice:
        return
    if choice.isdigit() and 1 <= int(choice) <= len(books):
        book = os.path.join(BOOKS_DIR, books[int(choice) - 1])
    elif os.path.exists(choice):
        book = choice
    else:
        print("  没找到这本书。")
        return
    prog = load_progress().get(os.path.abspath(book))
    start = 0
    if prog and prog.get("idx", 0) > 0:
        ans = input(f"  发现进度（第 {prog['idx']+1} 段），回车继续 / 输入 r 从头朗读: ").strip()
        start = 0 if ans.lower() == "r" else prog["idx"]
    read_book(book, start_idx=start)


# ---------------------------------------------------------------- 音色设置


def voice_settings():
    v = CFG["voice"]
    print("\n── 音色设置 ─────────────────────────────")
    print(f"  当前: {v['name']}  语速 {v.get('speed', 1.0)}")
    print(f"  描述: {v['instructions'][:50]}\n")
    for key, (name, _) in VOICE_PRESETS.items():
        cur = " ←当前" if name == v["name"] else ""
        print(f"  {key}. {name}{cur}")
    print("  8+. 自定义（中英文描述均可，如「低沉磁性的大叔音」）")
    choice = input("\n  选择（回车返回）: ").strip()
    if not choice:
        return
    if choice in VOICE_PRESETS:
        name, instructions = VOICE_PRESETS[choice]
    else:
        name = "自定义"
        instructions = input("  输入音色描述: ").strip()
        if not instructions:
            print("  描述为空，取消。")
            return
    speed_input = input(f"  语速 0.5~1.5（回车保持 {v.get('speed', 1.0)}）: ").strip()
    try:
        speed = min(1.5, max(0.5, float(speed_input))) if speed_input else v.get("speed", 1.0)
    except ValueError:
        speed = v.get("speed", 1.0)

    print("  合成试听…")
    try:
        speak("你好，这是我的声音，喜欢的话就选我吧。", voice={"instructions": instructions, "speed": speed})
    except Exception as e:
        print(f"  ⚠ 试听失败: {e}")
        return
    if input("  满意吗？(y 保存 / 回车重选) ").strip().lower() == "y":
        CFG["voice"] = {"name": name, "instructions": instructions, "speed": speed}
        path = os.path.join(HERE, "config.json")
        cfg_disk = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                cfg_disk = json.load(f)
        cfg_disk["voice"] = CFG["voice"]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg_disk, f, ensure_ascii=False, indent=2)
        print(f"  ✅ 已保存音色「{name}」。\n  （对话与朗读都会使用新音色；旧段缓存重读时生效）")
    else:
        voice_settings()


# ---------------------------------------------------------------- 自检 & 主菜单


def selftest():
    print("═" * 50)
    print("  语音伴侣 · 全链路自检（omlx 本地最小模型）")
    print("═" * 50)
    print(f"  识别: {CFG['asr_model']}")
    print(f"  对话: {CFG['chat_model']}")
    print(f"  合成: {CFG['tts_model']}")
    print(f"  音色: {CFG['voice']['name']}\n")

    print("① 对话模型测试…", flush=True)
    reply = think("用一句话打个招呼")
    print(f"   ✔ {reply[:80]}")

    print("② 语音合成试听（当前音色）…", flush=True)
    speak("你好，我是小伴。当前音色试听，接下来测试朗读功能。")
    print("   ✔ 已播放")

    print("③ 朗读管线测试…", flush=True)
    sample = os.path.join(BOOKS_DIR, "示例.txt")
    if not os.path.exists(sample):
        os.makedirs(BOOKS_DIR, exist_ok=True)
        with open(sample, "w", encoding="utf-8") as f:
            f.write("语音伴侣使用说明。\n\n第一段：本系统全部运行在本机 omlx 服务上，"
                    "不联网、不产生云端费用。识别与合成各为一点七B的小模型，"
                    "对话使用九B模型，是本机最小的组合。\n\n"
                    "第二段：把你想读的书放进 books 文件夹，支持 txt、pdf、word 等格式，"
                    "朗读进度会自动保存，下次可以接着听。\n")
    text = extract_text(sample)
    segs = split_segments(text, CFG["segment_max_chars"])
    print(f"   ✔ 解析出 {len(segs)} 段")
    tmp = tempfile.mkdtemp(prefix="vc_st_")
    try:
        wav = os.path.join(tmp, "0.wav")
        tts_to_file(segs[0], wav)
        subprocess.run(["afplay", wav], check=False)
        print("   ✔ 第一段已合成并播放")
        back = transcribe(wav)
        print(f"   ✔ 识别回听: {back[:40]}")
    finally:
        for f in os.listdir(tmp):
            try:
                os.remove(os.path.join(tmp, f))
            except OSError:
                pass
        os.rmdir(tmp)
    print("\n✅ 自检通过：对话 / 合成 / 朗读管线均正常。")
    print("   运行 python3 voice_companion.py 进入菜单。")


def main():
    # 后台/非交互 shell 启动时 SIGINT 可能被设为忽略，这里恢复默认的 Ctrl+C 行为
    try:
        if signal.getsignal(signal.SIGINT) == signal.SIG_IGN:
            signal.signal(signal.SIGINT, signal.default_int_handler)
    except (ValueError, OSError):
        pass
    args = sys.argv[1:]
    if "--selftest" in args:
        selftest()
        return
    if "--chat" in args:
        chat_mode()
        return
    if "--read" in args and len(args) > args.index("--read") + 1:
        book = args[args.index("--read") + 1]
        start = 0
        prog = load_progress().get(os.path.abspath(book))
        if prog and prog.get("idx", 0) > 0 and "reset" not in args:
            start = prog["idx"]
            print(f"🔖 从上次进度继续: 第 {start+1}/{prog['total']} 段（从头读请加 reset 参数）")
        read_book(book, start_idx=start)
        return
    while True:
        print("\n╔══════════════════════════════════════╗")
        print("║   语音伴侣 · 本地朗读与对话 (omlx)    ║")
        print("╚══════════════════════════════════════╝")
        print(f"  当前音色: {CFG['voice']['name']}  语速 {CFG['voice'].get('speed', 1.0)}")
        print("  1. 朗读模式（听书，自动记进度）")
        print("  2. 对话模式（和小伴聊天）")
        print("  3. 音色设置（预置 8 种 / 自定义描述）")
        print("  4. 自检")
        print("  0. 退出")
        try:
            choice = input("  选择: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n再见！")
            return
        if choice == "1":
            reading_mode()
        elif choice == "2":
            chat_mode()
        elif choice == "3":
            voice_settings()
        elif choice == "4":
            selftest()
        elif choice == "0":
            print("再见！")
            return


if __name__ == "__main__":
    main()
