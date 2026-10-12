#!/usr/bin/env bash
# 强平 judge 异步服务的启停脚本。放在仓库根目录，在服务器上 git clone 下来直接用。
#
#   bash serve.sh              启动（已在跑就什么都不做，可以放进 crontab 反复执行）
#   bash serve.sh stop         停止
#   bash serve.sh restart      重启（改了 service.keys 或拉了新代码之后）
#   bash serve.sh status       是否在跑 + 健康检查
#   bash serve.sh logs         跟日志
#   bash serve.sh attach       进 tmux 看实时输出（Ctrl-b 再按 d 退出，服务不停）
#   bash serve.sh check        部署后先跑一次：自检 + 真调一次模型端点
#   bash serve.sh run          前台跑一次、不自动重启（给 systemd 用，或者调试）
#
# 进程怎么不被杀：
#   1 有 tmux 就开在 tmux 会话里，关掉 SSH 不影响；没有 tmux 就用 setsid + nohup 脱离终端
#   2 会话里是一个重启循环，Python 进程崩了 5 秒后自动拉起
#   3 机器重启 / tmux 本身被杀：crontab 里每 5 分钟跑一次 `bash serve.sh`，见 docs/service.md
#
# 可以用环境变量改：LIQ_JUDGE_HOST LIQ_JUDGE_PORT LIQ_JUDGE_WORKERS LIQ_JUDGE_DATA LIQ_JUDGE_LLM LIQ_JUDGE_KEYS_FILE PYTHON
set -euo pipefail

cd "$(dirname "$0")"
SELF="$(pwd)/serve.sh"
PY="${PYTHON:-python3}"
HOST="${LIQ_JUDGE_HOST:-0.0.0.0}"
PORT="${LIQ_JUDGE_PORT:-8788}"
WORKERS="${LIQ_JUDGE_WORKERS:-4}"
DATA="${LIQ_JUDGE_DATA:-data}"
LLM="${LIQ_JUDGE_LLM:-glm}"
SESSION="liq-judge"
KEYS_FILE="${LIQ_JUDGE_KEYS_FILE:-service.keys}"
LOG="$DATA/serve.log"
PIDFILE="$DATA/serve.pid"
LOG_MAX_BYTES=$((50 * 1024 * 1024))

mkdir -p "$DATA"

say() { printf '%s\n' "$*"; }

need_python() {
  if ! command -v "$PY" >/dev/null 2>&1; then
    say "找不到 $PY。装 Python 3.9+，或者 PYTHON=/path/to/python3 bash serve.sh"
    exit 1
  fi
  if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    say "Python 版本太老：$("$PY" -V 2>&1)，需要 3.9+"
    exit 1
  fi
}

# 调用方的 key：一行一个「名字:密钥」。第一次启动没有就生成一个。
# 这个文件不进 git（.gitignore），换 key 直接改它，然后 bash serve.sh restart。
load_keys() {
  if [ ! -s "$KEYS_FILE" ]; then
    local key
    key="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(24))')"
    ( umask 077
      { echo "# 调用强平 judge 服务的 API key，一行一个：名字:密钥"
        echo "# 调用方请求时带 Authorization: Bearer <密钥>；名字只用来区分谁的任务"
        echo "# 改完执行 bash serve.sh restart 生效"
        echo "default:${key}"; } > "$KEYS_FILE" )
    say "生成了 $KEYS_FILE，里面有一个调用方 default 的 key（cat $KEYS_FILE 查看，换成你自己的也行）"
  fi
  LIQ_JUDGE_KEYS="$(grep -v '^[[:space:]]*#' "$KEYS_FILE" | sed 's/[[:space:]]//g' \
                        | grep -v '^$' | paste -sd, -)"
  if [ -z "$LIQ_JUDGE_KEYS" ]; then
    say "$KEYS_FILE 里没有有效的 key（格式：名字:密钥）"
    exit 1
  fi
  export LIQ_JUDGE_KEYS
}

has_tmux() { command -v tmux >/dev/null 2>&1; }

running() {
  if has_tmux && tmux has-session -t "$SESSION" 2>/dev/null; then
    return 0
  fi
  [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

rotate_log() {
  if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt "$LOG_MAX_BYTES" ]; then
    mv -f "$LOG" "$LOG.1"
  fi
}

serve_once() {
  local exec_or_run=${1:-run}
  local args=(cli.py serve --host "$HOST" --port "$PORT" --workers "$WORKERS"
              --llm "$LLM" --data "$DATA")
  if [ "$exec_or_run" = exec ]; then
    exec "$PY" -u "${args[@]}"
  fi
  "$PY" -u "${args[@]}"
}

# 循环自己的提示：tmux 里屏幕和日志各一份；nohup 里 stdout 本来就是日志，别写两遍
note() {
  if [ -n "${TMUX:-}" ]; then say "$*" | tee -a "$LOG"; else say "$*"; fi
}

# 重启循环：跑在 tmux 会话或 nohup 进程里
loop() {
  need_python
  load_keys
  while true; do
    rotate_log
    note "$(date '+%F %T') 启动强平 judge 服务 ${HOST}:${PORT}"
    set +e
    if [ -n "${TMUX:-}" ]; then
      serve_once 2>&1 | tee -a "$LOG"          # tmux 里：屏幕上看得到，日志里也有
      code=${PIPESTATUS[0]}
    else
      serve_once >> "$LOG" 2>&1                 # nohup 里：stdout 本来就是日志
      code=$?
    fi
    set -e
    note "$(date '+%F %T') 服务退出（退出码 $code），5 秒后重启"
    sleep 5
  done
}

start() {
  need_python
  if running; then
    say "已经在跑了。bash serve.sh status 看状态"
    return 0
  fi
  load_keys                                     # 在这里先生成 key，提示能打在当前终端上
  if has_tmux; then
    # tmux 服务端早就开着的话，新会话拿不到当前 shell 的环境变量 —— 显式带过去
    tmux new-session -d -s "$SESSION" \
      "env LIQ_JUDGE_HOST='$HOST' LIQ_JUDGE_PORT='$PORT' LIQ_JUDGE_WORKERS='$WORKERS' LIQ_JUDGE_DATA='$DATA' \
           LIQ_JUDGE_LLM='$LLM' LIQ_JUDGE_KEYS_FILE='$KEYS_FILE' PYTHON='$PY' bash '$SELF' _loop"
    say "已在 tmux 会话 $SESSION 里启动：bash serve.sh attach 看实时输出"
  else
    if command -v setsid >/dev/null 2>&1; then
      setsid nohup bash "$SELF" _loop >> "$LOG" 2>&1 < /dev/null &
    else
      nohup bash "$SELF" _loop >> "$LOG" 2>&1 < /dev/null &
    fi
    echo $! > "$PIDFILE"
    say "没装 tmux，已用 nohup 后台启动（pid $(cat "$PIDFILE")）：bash serve.sh logs 看日志"
  fi
  sleep 2
  status || true
}

stop() {
  local stopped=0
  if has_tmux && tmux has-session -t "$SESSION" 2>/dev/null; then
    tmux kill-session -t "$SESSION"
    stopped=1
  fi
  if [ -f "$PIDFILE" ]; then
    local pid
    pid="$(cat "$PIDFILE")"
    if kill -0 "$pid" 2>/dev/null; then
      # setsid 起的是独立进程组，整组一起停；否则先停循环、再停它的子进程
      kill -- "-$pid" 2>/dev/null || { pkill -P "$pid" 2>/dev/null; kill "$pid" 2>/dev/null; } || true
      stopped=1
    fi
    rm -f "$PIDFILE"
  fi
  # 兜底：循环已经没了但 Python 还占着端口
  pkill -f "cli.py serve --host $HOST --port $PORT " 2>/dev/null && stopped=1 || true
  if [ "$stopped" = 1 ]; then
    say "已停止。跑到一半的任务下次启动时接着跑，已判完的轮次不重判"
  else
    say "本来就没在跑"
  fi
}

status() {
  if running; then
    if has_tmux && tmux has-session -t "$SESSION" 2>/dev/null; then
      say "在跑（tmux 会话 $SESSION）"
    else
      say "在跑（nohup，pid $(cat "$PIDFILE")）"
    fi
  else
    say "没在跑"
    return 1
  fi
  local body
  if body="$(curl -s --noproxy '*' --max-time 5 "http://127.0.0.1:${PORT}/healthz")"; then
    say "健康检查：$body"
  else
    say "进程在，但 127.0.0.1:${PORT} 没应答 —— 可能还在启动，或者端口被占了。bash serve.sh logs 看看"
  fi
}

check() {
  need_python
  say "== 自检（不打模型端点）=="
  "$PY" cli.py selftest | tail -3
  say ""
  say "== 真调一次模型端点 =="
  "$PY" cli.py probe
}

case "${1:-start}" in
  start)    start ;;
  stop)     stop ;;
  restart)  stop; sleep 1; start ;;
  status)   status ;;
  logs)     touch "$LOG"; tail -n 100 -f "$LOG" ;;
  attach)   if has_tmux; then tmux attach -t "$SESSION"; else say "没装 tmux，用 bash serve.sh logs"; fi ;;
  check)    check ;;
  run)      need_python; load_keys; serve_once exec ;;
  _loop)    loop ;;
  *)        sed -n '2,20p' "$SELF"; exit 1 ;;
esac
