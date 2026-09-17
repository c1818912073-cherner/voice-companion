<div align="center">
  <img src="web/logo.svg" width="96" alt="语音伴侣 Voice Companion">
  <h1>语音伴侣 · Voice Companion</h1>
  <p><strong>全离线本地朗读与对话软件</strong> —— 分角色听书 · 语音聊天 · 文字定制音色</p>

  [![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
  ![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
  ![Platform](https://img.shields.io/badge/Platform-macOS%20Apple%20Silicon-000000?logo=apple&logoColor=white)
  ![Offline](https://img.shields.io/badge/隐私-100%25%20本地离线-brightgreen)
  ![Deps](https://img.shields.io/badge/依赖-仅%20Python%20标准库-8b7cf8)
</div>

---

语音识别、语音合成、对话模型全部运行在本机 [omlx](https://github.com/omlx-ai/omlx) 服务上，选用**同类最小的三个本地模型**，普通 Apple Silicon Mac 即可流畅运行：

| 环节 | 模型 | 参数量 |
|---|---|---|
| 语音识别 | `Qwen3-ASR-1.7B` | 1.7B |
| 语音合成 | `Qwen3-TTS-12Hz-1.7B-VoiceDesign` | 1.7B（音色可用文字描述） |
| 对话 | `Qwythos-9B` | 9B（本机最小的 LLM） |

> 全程离线：书籍、语音、聊天记录都不离开你的电脑。

## 界面预览

| 📖 分角色朗读 | 💬 语音对话 | 🎙 音色库 |
|---|---|---|
| ![朗读](docs/screenshots/read.png) | ![对话](docs/screenshots/chat.png) | ![音色](docs/screenshots/voices.png) |

## 特性

- **📖 分角色朗读（听书）**
  - 支持 **txt / md / pdf / doc / docx / rtf / html**
  - 规则引擎把小说切成「旁白 / 角色 / 台词」，不同人物配不同音色——像广播剧一样听书
  - LLM 一键「深度识别人物」（含性别），自动按性别分配音色
  - 边播边预取下一段，几乎无停顿；点击任意段落跳读
  - 进度自动记忆，换音色后缓存自动失效重新合成
  - 一键**导出整本有声书 mp3**
- **💬 语音对话**
  - 点麦克风说话：录音 → 识别 → 回复 → 朗读，一气呵成
  - 勾选**连续对话**后免点击轮流交流；保留最近 20 轮记忆，可一键清空
  - 也可以直接打字
  - **回复带人物对话时自动分角色朗读**：先切分成「旁白 / 角色 / 台词」，每个角色用不同音色逐句播读、当前句高亮，像广播剧一样听故事
- **🎙 音色定制（VoiceDesign 的核心玩法）**
  - 8 种预置音色 + 自定义描述，**用中文写一句话就能创造新音色**：
    > `低沉磁性的大叔音，慢慢说话` · `像闺蜜一样轻快亲切的女声` · `严肃的老教授在讲课的语气`
  - 先试听再保存，语速 0.5~1.5 可调；对话与朗读共用音色库
- **🔒 隐私**：无账号、无云端、无遥测，所有数据存于本地 `books/` 与 `voices.json`

## 快速开始

### 依赖

- macOS（Apple Silicon）+ [omlx](https://github.com/omlx-ai/omlx) 正在运行（默认 `127.0.0.1:8880`）
- `ffmpeg`：仅用于浏览器录音转码与有声书导出（`brew install ffmpeg`）
- Python 3.10+：**无需安装任何 pip 包**，仅标准库

### Web 版（推荐）

```bash
git clone <你的 fork> && cd voice-companion
python3 web_server.py            # 打开 http://127.0.0.1:8890
```

macOS 也可以直接双击 `启动Web语音伴侣.command`（自动打开浏览器）。三个页签可用 `#read` `#chat` `#voice` 直达链接。

### 终端版

```bash
python3 voice_companion.py --selftest   # 先自检（会外放两句试音）
python3 voice_companion.py              # 进入菜单
python3 voice_companion.py --chat       # 直接进对话
python3 voice_companion.py --read 路径/书.txt   # 直接开读某本书
```

## 工作原理

```
┌─────────────┐   HTTP    ┌──────────────────┐   OpenAI 兼容 API   ┌────────────┐
│  web/ 前端   │ ────────▶ │   web_server.py   │ ─────────────────▶ │   omlx     │
│ (浏览器 UI)  │ ◀──────── │ (音色/书籍/缓存)   │ ◀───────────────── │ 本地模型服务 │
└─────────────┘  wav/json └───────┬──────────┘   ASR / TTS / Chat   └────────────┘
                                  │ 复用
                    ┌─────────────┴──────────┐
                    │  voice_companion.py     │  录音 / 识别 / 合成 / 对话 / 进度
                    │  novel_engine.py        │  小说分角色切分 + 人物识别
                    └─────────────────────────┘
```

- **novel_engine.py**：纯正则把小说文本切成「说话人 + 台词」脚本；`build_characters()` 用一次 LLM 调用通读全书识别人物与性别（结果缓存，可离线跳过）
- **web_server.py**：标准库 `ThreadingHTTPServer`，零依赖；按「书籍内容哈希 + 音色签名」缓存合成音频，改音色只重合成受影响的书

## HTTP API

`web_server.py` 同时是一个可独立使用的本地 API 服务：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state` | 音色库 / 书架 / 当前模型 |
| GET | `/api/book?path=…` | 书籍脚本（台词行 + 人物 + 进度） |
| POST | `/api/book/analyze` | 深度识别人物（LLM，一次） |
| POST | `/api/book/cast` | 保存角色 → 音色映射 |
| POST | `/api/segment` | 合成指定段落（wav，带缓存） |
| POST | `/api/tts` ` /api/asr` ` /api/chat` | 合成 / 识别 / 对话 |
| POST | `/api/script` | 任意文本切分为「说话人+台词」（对话分角色朗读用） |
| POST | `/api/export` | 导出整本有声书 mp3（异步任务） |
| POST | `/api/books/upload` ` /api/books/delete` | 上传 / 删除书籍 |
| POST | `/api/voices/save` ` /delete` ` /current` | 音色库管理 |

## 配置（config.json）

| 键 | 说明 |
|---|---|
| `base_url` | omlx 服务地址，默认 `http://127.0.0.1:8880/v1` |
| `voice` | 当前音色（Web 页签里改更方便） |
| `segment_max_chars` | 朗读分段长度，默认 280 字 |
| `prefetch_segments` | 预合成段数，内存紧张可降为 2 |
| `silence_threshold_pct` | 环境吵→调高到 4；很安静→降到 1.5 |
| `silence_stop_seconds` | 停顿多久算说完，默认 1.4 秒 |
| `chat_model` | 想更聪明可换更大的模型（如 `Qwen3.6-35B-A3B-4bit`） |

## 项目结构

```
voice-companion/
├── web_server.py        # Web 服务器 + HTTP API（标准库）
├── voice_companion.py   # 终端版 + 核心能力（录音/识别/合成/对话）
├── novel_engine.py      # 小说分角色切分 + 人物识别
├── web/index.html       # 单页前端（无构建、无框架）
├── config.json          # 默认配置
├── books/               # 书库与听书进度（git 忽略）
├── docs/screenshots/    # README 截图
└── 启动*.command         # macOS 双击启动
```

## 已知限制

- 朗读暂停后恢复会从**本段开头**重播（系统播放器不支持从中途续播）
- 首次对话会弹出麦克风授权，请允许
- 仅监听 `127.0.0.1`，适合本机使用；如需局域网访问请自行加鉴权

## Roadmap

- [ ] 章节导航 / 书签
- [ ] 更多书籍格式的目录解析（epub）
- [ ] 声音克隆（给定参考音频定制音色）
- [ ] 对话内容导出

## License

[MIT](LICENSE) · 欢迎提 Issue 和 PR
