#!/bin/bash
# 双击启动语音伴侣
cd "$(dirname "$0")"
python3 voice_companion.py
echo ""
echo "已退出，3 秒后关闭窗口…"
sleep 3
