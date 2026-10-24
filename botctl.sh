#!/usr/bin/env bash
set -u

DIR="/root/offer/gifts-market-demo-bot"
PYTHON="/usr/bin/python3"
MASTER_PYTHON="/root/venv/bin/python3"
SETSID="/usr/bin/setsid"
LOCK_FILE="/tmp/offer-demo-bots.lock"
FREE_LOG="/tmp/offer-free-bot.log"
PAID_LOG="/tmp/offer-paid-bot.log"
MASTER_LOG="/tmp/offer-master-bot.log"

matches_bot() {
    local pid="$1"
    local script="$2"
    local cwd
    local arg
    local -a argv

    [[ -r "/proc/$pid/cmdline" ]] || return 1
    cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null)" || return 1
    [[ "$cwd" == "$DIR" ]] || return 1
    mapfile -d '' -t argv < "/proc/$pid/cmdline" 2>/dev/null || return 1

    for arg in "${argv[@]}"; do
        [[ "$arg" == "$script" || "$arg" == "$DIR/$script" ]] && return 0
    done
    return 1
}

find_bot_pid() {
    local script="$1"
    local proc
    local pid

    for proc in /proc/[0-9]*; do
        pid="${proc##*/}"
        matches_bot "$pid" "$script" && {
            printf '%s\n' "$pid"
            return 0
        }
    done
    return 1
}

acquire_lock() {
    exec 9>"$LOCK_FILE"
    flock -n 9
}

start_one() {
    local script="$1"
    local log="$2"
    local title="$3"
    local python="${4:-$PYTHON}"
    local pid

    if pid="$(find_bot_pid "$script")"; then
        [[ "${QUIET:-0}" == 1 ]] || echo "✓ $title уже работает (pid $pid)"
        return 0
    fi

    cd "$DIR" || return 1
    # Run outside the command's process group. The execution host may clean up
    # background jobs when this script exits; a separate session keeps the bot
    # alive after `botctl.sh` returns.
    nohup "$SETSID" "$python" -u "$DIR/$script" >>"$log" 2>&1 </dev/null 9>&- &
    pid=$!
    echo "✓ $title запускается (pid $pid)"
}

stop_one() {
    local script="$1"
    local title="$2"
    local pid
    local waited=0

    if ! pid="$(find_bot_pid "$script")"; then
        echo "· $title не работает"
        return 0
    fi

    kill -TERM "$pid" 2>/dev/null || true
    while kill -0 "$pid" 2>/dev/null && [[ "$waited" -lt 10 ]]; do
        sleep 1
        waited=$((waited + 1))
    done
    echo "✓ $title остановлен"
}

status_all() {
    local pid
    if pid="$(find_bot_pid bot.py)"; then
        echo "✓ основной бот работает (pid $pid)"
    else
        echo "· основной бот не работает"
    fi

    if pid="$(find_bot_pid paid_bot.py)"; then
        echo "✓ платный бот работает (pid $pid)"
    else
        echo "· платный бот не работает"
    fi

    if pid="$(find_bot_pid master_bot.py)"; then
        echo "✓ главный бот-конструктор работает (pid $pid)"
    else
        echo "· главный бот-конструктор не работает"
    fi
}

start_master() {
    if [[ -f "$DIR/.env" ]] && grep -Eq '^MASTER_BOT_TOKEN=[^[:space:]]' "$DIR/.env"; then
        start_one master_bot.py "$MASTER_LOG" "главный бот-конструктор" "$MASTER_PYTHON"
    elif [[ "${QUIET:-0}" != 1 ]]; then
        echo "· главный бот-конструктор не запущен: заполните MASTER_BOT_TOKEN в .env"
    fi
}

command="${1:-autostart}"
case "$command" in
    start|autostart|restart|stop)
        acquire_lock || exit 0
        ;;
esac

case "$command" in
    start)
        start_one bot.py "$FREE_LOG" "основной бот"
        start_one paid_bot.py "$PAID_LOG" "платный бот"
        start_master
        ;;
    autostart)
        QUIET=1 start_one bot.py "$FREE_LOG" "основной бот"
        QUIET=1 start_one paid_bot.py "$PAID_LOG" "платный бот"
        QUIET=1 start_master
        ;;
    stop)
        stop_one master_bot.py "главный бот-конструктор"
        stop_one bot.py "основной бот"
        stop_one paid_bot.py "платный бот"
        ;;
    restart)
        stop_one master_bot.py "главный бот-конструктор"
        stop_one bot.py "основной бот"
        stop_one paid_bot.py "платный бот"
        start_one bot.py "$FREE_LOG" "основной бот"
        start_one paid_bot.py "$PAID_LOG" "платный бот"
        start_master
        ;;
    status)
        status_all
        ;;
    *)
        echo "Команды: start | stop | restart | status | autostart"
        exit 1
        ;;
esac
