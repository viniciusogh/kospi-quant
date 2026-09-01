#!/bin/bash
# 일일 아카이브 — launchd(com.vinicius.daily-archive)가 평일 21:00 에 호출.
# 21:00 인 이유: 유튜브(20:00)·모멘텀(17:30)·섹터(17:11) 가 모두 끝난 뒤 종합해야 한다.
cd /Users/vinicius/Projects/kospi-quant || exit 1
LOG=archive_cron.log
[ "$(date +%u)" -gt 5 ] && exit 0

set -a
. ./.env
set +a

echo "[$(date '+%Y-%m-%d %H:%M:%S')] ─── 실행 ───" >> "$LOG"
# 하드 상한 900s — 응답 없는 API 에 물려 좀비로 남으면 launchd 가 이후 실행을 막는다
perl -e 'alarm shift; exec @ARGV' 900 /opt/homebrew/bin/python3 daily_archive.py >> "$LOG" 2>&1
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 종료(exit=$?)" >> "$LOG"

# 과거 추천의 수익률 갱신 (같은 실행에서)
perl -e 'alarm shift; exec @ARGV' 600 /opt/homebrew/bin/python3 daily_archive.py --eval >> "$LOG" 2>&1

if [ "$(wc -l < "$LOG")" -gt 1000 ]; then tail -500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"; fi
