#!/bin/bash
# Example print_format / context_format script
# Env vars: FROM=发送者, TYPE=消息类型, TEXT=消息正文
# stdout → rendered output (supports ANSI colors)

GREEN="\033[1;32m"
YELLOW="\033[1;33m"
RED="\033[1;31m"
CYAN="\033[0;36m"
RESET="\033[0m"

case "$TYPE" in
  task_done)
    echo -e "${GREEN}✔ ${FROM}${RESET} — ${TEXT}"
    ;;
  plan_ready)
    echo -e "${YELLOW}📋 ${FROM} 方案已出${RESET}\n   ${TEXT}"
    ;;
  task_error)
    echo -e "${RED}✖ ${FROM} 异常${RESET}\n   ${TEXT}"
    ;;
  need_decision)
    echo -e "${YELLOW}⚠ ${FROM} 需要决策${RESET}\n   ${TEXT}"
    ;;
  *)
    echo -e "${CYAN}${FROM}${RESET}: ${TEXT}"
    ;;
esac
