#!/bin/bash
# 双击这个文件启动“字幕翻译工具”。
# 会打开一个终端窗口（工具的后台），并自动在浏览器里打开操作界面。
# 用完后关掉这个终端窗口，或在界面右上角点“退出”。
#
# 第一次启动时如果还没有运行环境（runtime/ 文件夹），会先自动安装（约 1 GB）。

cd "$(dirname "$0")" || exit 1
PY="./runtime/bin/python"

if [ ! -x "$PY" ]; then
  echo
  echo "  第一次使用，需要先安装运行环境：约 1 GB，需要联网，一般 5～10 分钟。"
  echo "  全部装在本文件夹的 runtime/ 里，不需要密码，不影响系统里的其它软件。"
  echo "  (First run: the runtime, about 1 GB, needs to be installed first.)"
  echo
  read -r -p "  按回车键开始安装；不想装就直接关掉这个窗口… (Press Return to install) " _
  if ! SUBTITLE_TOOL_NO_REVEAL=1 bash ./install.sh; then
    echo
    read -r -p "安装没有成功，可以检查网络后再双击一次。按回车键关闭…" _
    exit 1
  fi
fi

printf '\033]0;字幕翻译工具\007'
exec "$PY" -m subtitle_tool "$@"
