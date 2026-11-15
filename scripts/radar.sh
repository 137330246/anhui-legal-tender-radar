#!/bin/sh
# 安徽省法律服务招标信息收集和推送skill · 启动脚本（macOS / Linux / Git Bash）
# 自动寻找 Python 3.9+ 并运行 radar.py；找不到时打印安装方法，退出码 127。
# 可用环境变量 RADAR_PYTHON 指定 Python 路径。

HERE=$(cd "$(dirname "$0")" && pwd)

ok() {
  "$@" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1
}

for c in "$RADAR_PYTHON" python3 python py \
  "/Applications/WorkBuddy.app/Contents/Resources/vendor/python/bin/python3" \
  "$HOME/.local/share/WorkBuddy/resources/vendor/python/bin/python3" \
  "${LOCALAPPDATA:+$LOCALAPPDATA/Programs/WorkBuddy/resources/vendor/python/python.exe}"; do
  [ -z "$c" ] && continue
  if ok "$c"; then
    exec "$c" -X utf8 "$HERE/radar.py" "$@"
  fi
done

cat >&2 <<'EOF'
[PYTHON_NOT_FOUND] 未找到 Python 3.9 或更高版本，无法运行本 skill 的脚本。

安装方法（任选其一，装好后重新打开终端或 Agent）：
  macOS：   在终端运行  xcode-select --install
            或从 https://www.python.org/downloads/ 下载安装包
  Linux：   sudo apt install python3      （Debian / Ubuntu）
            sudo dnf install python3      （Fedora / CentOS）
  已有 Python 但不在 PATH 中：设置环境变量 RADAR_PYTHON=/path/to/python3

不想安装：可以让 Agent 使用「简易模式」（见 references/no-python.md），
只用 Agent 自带的联网搜索，覆盖面和可靠性较低，且不能自动推送。
EOF
exit 127
