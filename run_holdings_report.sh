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
# 하드 상한 1800s — 응답 없는 API 에 물려 좀비로 남으면 launchd 가 이후 실행을 전부 막는다
# (2026-08-28 holdings_report 가 Gemini 연결에 3일 6시간 물려 4일치 리포트가 누락됐다).
# macOS 기본 bash 엔 timeout(1) 이 없어 perl alarm 을 쓴다.
perl -e 'alarm shift; exec @ARGV' 1800 /opt/homebrew/bin/python3 holdings_report.py >> "$LOG" 2>&1
rc=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 종료(exit=$rc)" >> "$LOG"
[ "$rc" -eq 142 ] && echo "  🚨 하드 상한 초과로 강제 종료 — 원인 확인 필요" >> "$LOG"

if [ "$(wc -l < "$LOG")" -gt 1000 ]; then
    tail -500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

# 타임아웃(142)·실패를 0 으로 마스킹하면 launchd 가 성공으로 본다(Codex 지적).
exit "${rc:-0}"
