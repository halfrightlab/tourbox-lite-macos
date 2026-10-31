#!/bin/zsh
# 卸载登录自启服务（不删除源码和个人配置）
LABEL="com.tourbox.driver"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "已卸载服务。~/.tourbox 下的日志和 venv 如不需要可手动删除。"
