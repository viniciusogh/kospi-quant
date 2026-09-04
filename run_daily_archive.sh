#!/bin/bash
# 일일 아카이브 — launchd(com.vinicius.daily-archive)가 평일 18:00 에 호출.
# 18:00 인 이유: 모멘텀 산출물이 17:30~17:40 에 올라온다(실측). 장 마감 후 만들어
# 다음 거래일에 살 종목을 찍는다 (2026-09-04 규환 결정).
cd /Users/vinicius/Projects/kospi-quant || exit 1
LOG=archive_cron.log
[ "$(date +%u)" -gt 5 ] && exit 0

set -a
. ./.env
set +a

echo "[$(date '+%Y-%m-%d %H:%M:%S')] ─── 실행 ───" >> "$LOG"

# 실행 직전에 당긴다. 이 저장소를 pull 하는 스케줄이 아무 데도 없어서(2026-09-04 발견)
# 아카이브가 GitHub Actions 산출물을 못 받고 이틀 묵은 모멘텀으로 추천을 냈다.
# investment-chatbot 의 ETL 은 ~/Desktop/퀀트스코어 를 당기는데 그건 git 저장소가 아니다.
# 실패해도 진행한다 — 낡은 데이터로라도 페이지는 남기고, 낡음은 daily_archive 가 표시한다.
if git pull --rebase --autostash >> "$LOG" 2>&1; then
    echo "  ✅ git pull 완료" >> "$LOG"
else
    echo "  🚨 git pull 실패 — 낡은 입력으로 진행한다" >> "$LOG"
    # rebase 중간 상태가 남으면 이후 '모든' pull 이 unmerged files 로 막힌다(2026-09-04 재현).
    # 그대로 두면 아카이브가 영구히 낡은 데이터로 돈다 → 원상복구해 다음 실행이 다시 시도하게 한다.
    if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then
        if git rebase --abort >> "$LOG" 2>&1; then
            echo "  ↩︎ rebase 중단해 원상복구 (다음 실행에서 재시도)" >> "$LOG"
        else
            echo "  🚨 rebase 중단 실패 — 수동 확인 필요" >> "$LOG"
        fi
    fi
fi
# 하드 상한 900s — 응답 없는 API 에 물려 좀비로 남으면 launchd 가 이후 실행을 막는다
perl -e 'alarm shift; exec @ARGV' 900 /opt/homebrew/bin/python3 daily_archive.py >> "$LOG" 2>&1
rc=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 종료(exit=$rc)" >> "$LOG"
[ "$rc" -eq 142 ] && echo "  🚨 하드 상한 초과로 강제 종료 — 원인 확인 필요" >> "$LOG"

# 과거 추천의 수익률 갱신 (같은 실행에서)
perl -e 'alarm shift; exec @ARGV' 900 /opt/homebrew/bin/python3 daily_archive.py --eval >> "$LOG" 2>&1

if [ "$(wc -l < "$LOG")" -gt 1000 ]; then tail -500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"; fi

# 타임아웃(142)·실패를 0 으로 마스킹하면 launchd 가 성공으로 본다(Codex 지적).
exit "${rc:-0}"
