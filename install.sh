#!/bin/zsh
# TourBox 驱动 · 安装为登录自启服务（launchd LaunchAgent）
# 用法：在「终端」里执行 ./install.sh
set -eu

REPO="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="$HOME/.tourbox"
VENV="$HOME_DIR/venv"
LABEL="com.tourbox.driver"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT="${TOURBOX_PORT:-8777}"

mkdir -p "$HOME_DIR/logs" "$HOME/Library/LaunchAgents"
# 新装默认进入设置模式；重装时保留已有模式
[ -f "$HOME_DIR/mode" ] || echo setup > "$HOME_DIR/mode"

echo "→ 准备 Python 环境: $VENV"
if [ ! -x "$VENV/bin/python3" ]; then
  /usr/bin/python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$REPO/requirements.txt"

echo "→ 生成启动脚本"
cat > "$HOME_DIR/run.sh" <<RUN
#!/bin/zsh
# 由 install.sh 生成。日志超过 3MB 自动轮换。
LOG="$HOME_DIR/logs/driver.log"
if [ -f "\$LOG" ] && [ "\$(stat -f%z "\$LOG")" -gt 3000000 ]; then mv -f "\$LOG" "\$LOG.old"; fi
# 运行模式（~/.tourbox/mode）：
#   setup  驱动 + 网页控制台 + 按键统计（调键位阶段）
#   light  只跑驱动，不开网页、不占端口、不统计（方案定下来之后）
# 切换：./tourbox.sh setup | light
MODE="\$(cat "$HOME_DIR/mode" 2>/dev/null || echo setup)"
if [ "\$MODE" = "light" ]; then
  exec "$VENV/bin/python3" "$REPO/tourboxd.py"
fi
exec "$VENV/bin/python3" "$REPO/webui.py" --no-browser --port $PORT
RUN
chmod +x "$HOME_DIR/run.sh"

echo "→ 生成 launchd 服务: $PLIST"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$HOME_DIR/run.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$HOME_DIR/logs/driver.log</string>
  <key>StandardErrorPath</key><string>$HOME_DIR/logs/driver.err.log</string>
</dict>
</plist>
PL

echo "→ 退出 TourBox Console（它会独占串口）"
pkill -f "TourBox Console" 2>/dev/null || true
pkill -f "$REPO/webui.py" 2>/dev/null || true

echo "→ 注册服务"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 3
tail -8 "$HOME_DIR/logs/driver.log" 2>/dev/null || true

PYBIN="$("$VENV/bin/python3" -c 'import os,sys;print(os.path.realpath(sys.executable))')"
cat <<MSG

✅ 安装完成，现在是「设置模式」，控制台：http://127.0.0.1:$PORT/
   键位调好、统计看够了，点控制台右上角「完成设置」或执行 ./tourbox.sh light，
   之后只剩驱动在后台运行，网页和统计都会关掉。

如果日志里显示「辅助功能(注入)=未授权」：
  系统设置 → 隐私与安全性 → 辅助功能 → 点 + → ⌘⇧G 粘贴下面的路径并勾选：
  $PYBIN
然后执行：launchctl kickstart -k gui/$(id -u)/$LABEL
MSG
