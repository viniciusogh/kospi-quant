#!/bin/bash
# 포트폴리오 노션 갱신 — launchd(com.vinicius.portfolio)가 장중 30분마다 호출.
# launchd 는 .env 를 안 읽으므로 여기서 주입한다. 주말은 건너뜀(장 안 열림).
cd /Users/vinicius/Projects/kospi-quant || exit 1

LOG=portfolio_cron.log
DOW=$(date +%u)                      # 1=월 … 7=일
if [ "$DOW" -gt 5 ]; then
    exit 0                           # 주말 = 조용히 종료 (로그도 안 남김)
fi

set -a
# shellcheck disable=SC1091
. ./.env
set +a

echo "[$(date '+%Y-%m-%d %H:%M:%S')] ─── 실행 ───" >> "$LOG"
# 하드 상한 900s — 응답 없는 API 에 물려 좀비로 남으면 launchd 가 이후 실행을 전부 막는다
# (2026-08-28 holdings_report 가 Gemini 연결에 3일 6시간 물려 4일치 리포트가 누락됐다).
# macOS 기본 bash 엔 timeout(1) 이 없어 perl alarm 을 쓴다.
perl -e 'alarm shift; exec @ARGV' 780 /opt/homebrew/bin/python3 portfolio.py >> "$LOG" 2>&1
rc=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 종료(exit=$rc)" >> "$LOG"
[ "$rc" -eq 142 ] && echo "  🚨 하드 상한 초과로 강제 종료 — 원인 확인 필요" >> "$LOG"

# 로그 비대화 방지: 2000줄 넘으면 뒤 1000줄만 남김
if [ "$(wc -l < "$LOG")" -gt 2000 ]; then
    tail -1000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

# 타임아웃(142)·실패를 0 으로 마스킹하면 launchd 가 성공으로 본다(Codex 지적).
exit "${rc:-0}"
