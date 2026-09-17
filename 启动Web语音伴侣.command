#!/bin/bash
# 双击启动 Web 版语音伴侣（自动打开浏览器）
cd "$(dirname "$0")"
PORT="${1:-8890}"
python3 web_server.py "$PORT" &
SRV=$!
sleep 2
open "http://127.0.0.1:$PORT"
wait $SRV
