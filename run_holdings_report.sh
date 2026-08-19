#!/bin/bash
# 보유종목 심층분석 — launchd(com.vinicius.holdings-report)가 평일 16:10 에 호출.
# 16:10 인 이유: portfolio.py(16:00)가 portfolio.json 을 갱신한 뒤에 읽어야 한다.
cd /Users/vinicius/Projects/kospi-quant || exit 1

LOG=holdings_cron.log
[ "$(date +%u)" -gt 5 ] && exit 0      # 주말은 조용히 종료 (휴장일은 스크립트가 판정)

set -a
# shellcheck disable=SC1091
. ./.env
set +a

echo "[$(date '+%Y-%m-%d %H:%M:%S')] ─── 실행 ───" >> "$LOG"
/opt/homebrew/bin/python3 holdings_report.py >> "$LOG" 2>&1
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 종료(exit=$?)" >> "$LOG"

if [ "$(wc -l < "$LOG")" -gt 1000 ]; then
    tail -500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
