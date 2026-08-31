#!/bin/bash
# 섹터 장세 트리맵 — launchd(com.vinicius.sector)가 장중 15분마다 호출.
# 주말은 여기서 끊고, 대체공휴일은 스크립트의 market_open_today() 가 잡는다.
cd /Users/vinicius/Projects/kospi-quant || exit 1

LOG=sector_cron.log
[ "$(date +%u)" -gt 5 ] && exit 0      # 주말: 조용히 종료 (로그도 안 남김)

set -a
# shellcheck disable=SC1091
. ./.env
set +a

echo "[$(date '+%Y-%m-%d %H:%M:%S')] ─── 실행 ───" >> "$LOG"
# 하드 상한 1200s — 응답 없는 API 에 물려 좀비로 남으면 launchd 가 이후 실행을 전부 막는다
# (2026-08-28 holdings_report 가 Gemini 연결에 3일 6시간 물려 4일치 리포트가 누락됐다).
# macOS 기본 bash 엔 timeout(1) 이 없어 perl alarm 을 쓴다.
perl -e 'alarm shift; exec @ARGV' 1200 /opt/homebrew/bin/python3 sector_dashboard.py >> "$LOG" 2>&1
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 종료(exit=$?)" >> "$LOG"

if [ "$(wc -l < "$LOG")" -gt 3000 ]; then
    tail -1500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
