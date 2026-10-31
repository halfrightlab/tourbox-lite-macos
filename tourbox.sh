#!/bin/zsh
# TourBox 驱动 · 切换运行模式
#   ./tourbox.sh setup    设置模式：驱动 + 网页控制台 + 按键统计，并打开控制台
#   ./tourbox.sh light    轻量模式：只跑驱动，不开网页、不占端口、不统计
#   ./tourbox.sh status   看当前模式
set -u
HOME_DIR="$HOME/.tourbox"
LABEL="com.tourbox.driver"
MODE_FILE="$HOME_DIR/mode"

current() { cat "$MODE_FILE" 2>/dev/null || echo setup; }

restart() {
  if ! launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    echo "[!] 服务没有安装，先执行 ./install.sh"
    exit 1
  fi
  launchctl kickstart -k "gui/$(id -u)/$LABEL"
}

case "${1:-status}" in
  setup)
    echo setup > "$MODE_FILE"
    rm -f "$HOME_DIR/console_port"
    restart
    # 等控制台起来，端口被占用时它会自动换一个，实际端口写在 console_port 里
    for _ in {1..20}; do
      [ -s "$HOME_DIR/console_port" ] && break
      sleep 0.3
    done
    PORT="$(cat "$HOME_DIR/console_port" 2>/dev/null)"
    if [ -n "$PORT" ]; then
      echo "已切到设置模式，控制台：http://127.0.0.1:$PORT/"
      open "http://127.0.0.1:$PORT/"
    else
      echo "[!] 控制台没有起来，看日志：$HOME_DIR/logs/driver.log"
    fi
    ;;
  light)
    echo light > "$MODE_FILE"
    restart
    echo "已切到轻量模式：只剩驱动在后台运行，网页控制台和按键统计已关闭。"
    echo "以后要改键位：./tourbox.sh setup"
    ;;
  status)
    m="$(current)"
    if [ "$m" = "light" ]; then
      echo "轻量模式（只有驱动）"
    else
      PORT="$(cat "$HOME_DIR/console_port" 2>/dev/null)"
      echo "设置模式，控制台：http://127.0.0.1:${PORT:-8777}/"
    fi
    ;;
  *)
    echo "用法：./tourbox.sh setup | light | status"
    exit 2
    ;;
esac
