#!/bin/bash
# 리포트 추천 → 텔레그램 승인 → 한투 주문. launchd(com.vinicius.autotrade-*)가 호출.
#   run_auto_trade.sh propose   08:40  주문안 1회 전송
#   run_auto_trade.sh poll      09:00~15:20 매 5분  승인 확인 → 주문
# launchd 는 .env 를 안 읽으므로 여기서 주입한다. 주말은 조용히 종료.
cd /Users/vinicius/Projects/kospi-quant || exit 1

MODE="${1:-poll}"
LOG=auto_trade_cron.log
DOW=$(date +%u)                      # 1=월 … 7=일
[ "$DOW" -gt 5 ] && exit 0

set -a
# shellcheck disable=SC1091
. ./.env
set +a

echo "[$(date '+%Y-%m-%d %H:%M:%S')] ─── $MODE ───" >> "$LOG"
# 하드 상한 — 응답 없는 API 에 물리면 launchd 가 이후 실행을 전부 막는다
perl -e 'alarm shift; exec @ARGV' 240 /opt/homebrew/bin/python3 auto_trade.py "$MODE" >> "$LOG" 2>&1
rc=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 종료(exit=$rc)" >> "$LOG"
[ "$rc" -eq 142 ] && echo "  🚨 하드 상한 초과로 강제 종료 — 원인 확인 필요" >> "$LOG"

if [ "$(wc -l < "$LOG")" -gt 2000 ]; then
    tail -1000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

exit "${rc:-0}"
