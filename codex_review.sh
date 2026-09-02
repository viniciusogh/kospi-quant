#!/bin/bash
# Codex 에 검토를 자동으로 넘긴다. 작업 중이면 유휴가 될 때까지 재시도한다.
#
# dispatch --inject 는 상대가 턴 중이면 agent_prompt_blocked 로 거부된다(턴 끊김 방지).
# 그때 포기하면 사람이 Codex 창에 입력해줘야 한다 — 그건 자동화가 아니다(사용자 지적).
# 연달아 두드리면 작업이 failed 로 바뀌므로 간격을 두고 상한을 둔다.
#
# 사용: ./codex_review.sh <spec파일> [제목]
set -u
ME=term_d8eae294-5da0-4e6e-ad2d-2f7a7c66566d
export ME   # 파이프라인의 python3 가 상속받아야 한다
# 피어 핸들은 세션마다 바뀐다. 수신함의 최근 발신자로 찾고, 못 찾으면 마지막 known 값.
# 하드코딩만 두면 코덱스를 새로 띄운 순간 dispatch 가 조용히 실패한다 (2026-09-02).
PEER=${CODEX_PEER:-$(orca orchestration inbox --json 2>/dev/null | python3 _find_peer.py 2>/dev/null)}
PEER=${PEER:-term_bf5219d5-c750-4ae3-af13-2487dcb5f879}
SPEC="${1:?spec 파일 경로 필요}"
TITLE="${2:-클로드 검토 요청}"
LOG=codex_review.log
INTERVAL=${CODEX_RETRY_SEC:-45}
MAX=${CODEX_RETRY_MAX:-40}          # 45s × 40 = 30분
RESET_MAX=${CODEX_RESET_MAX:-3}      # 죽은 작업 되살리기 상한
WAIT_SEC=${CODEX_WAIT_SEC:-30}      # 회신 폴링 간격
WAIT_MAX=${CODEX_WAIT_MAX:-40}      # 30s × 40 = 20분

log(){ echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

TASK=$(orca orchestration task-create --from "$ME" --task-title "$TITLE" \
  --spec "$(cat "$SPEC")" --json 2>/dev/null \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['task']['id'])" 2>/dev/null)
[ -z "${TASK:-}" ] && { log "❌ task 생성 실패"; exit 1; }
log "task $TASK 생성 · 유휴 대기 시작 (최대 $((INTERVAL*MAX/60))분)"

RESETS=0
for i in $(seq 1 "$MAX"); do
  # 유휴 대기 중 재시도가 쌓이면 작업이 failed 로 바뀌고, 그 뒤 dispatch 는 전부
  # runtime_error 다. 2026-09-02 에 죽은 작업을 37번 더 두드리며 30분을 태웠다.
  ST=$(orca orchestration task-list --json 2>/dev/null | python3 _task_status.py "$TASK" 2>/dev/null)
  if [ "$ST" = "failed" ]; then
    RESETS=$((RESETS + 1))
    if [ "$RESETS" -gt "$RESET_MAX" ]; then
      log "❌ 작업이 ${RESET_MAX}회 죽었다 — 중단 (코덱스가 계속 바쁘거나 주입을 못 받는 상태)"
      exit 4
    fi
    orca orchestration task-update --id "$TASK" --status ready --from "$ME" >/dev/null 2>&1
    log "↻ 작업을 ready 로 되돌림 ($RESETS/$RESET_MAX)"
  fi
  RESP=$(orca orchestration dispatch --task "$TASK" --to "$PEER" --from "$ME" --inject --json 2>/dev/null)
  CODE=$(printf '%s' "$RESP" | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    print('OK' if d.get('ok') else d.get('error',{}).get('code','?'))
except Exception: print('parse')" 2>/dev/null)
  case "$CODE" in
    OK) log "✅ 주입 성공 (시도 $i) — 회신 대기"
        # 코덱스는 Run 으로 회신한다. 내 터미널 핸들로 조회하면 0건이 나온다.
        # 2026-09-02: 답이 와 있는데 '안 왔다' 고 오진했다 — 우편함을 잘못 열었다.
        SINCE=$(date -u "+%Y-%m-%dT%H:%M:%S")
        export PEER SINCE
        for j in $(seq 1 "$WAIT_MAX"); do
          BODY=$(orca orchestration inbox --json 2>/dev/null | python3 _reply_poll.py 2>/dev/null)
          if [ -n "$BODY" ]; then
            log "📥 회신 도착"
            printf '%s\n' "$BODY" | tee -a "$LOG"
            exit 0
          fi
          sleep "$WAIT_SEC"
        done
        log "⏰ $((WAIT_SEC*WAIT_MAX/60))분간 회신 없음 — 코덱스 창의 권한 프롬프트 확인"
        exit 3 ;;
    agent_prompt_blocked) sleep "$INTERVAL" ;;         # 작업 중 — 조용히 기다린다
    *) MSG=$(printf '%s' "$RESP" | python3 -c "
import json,sys
try: print(json.load(sys.stdin).get('error',{}).get('message','')[:150])
except Exception: pass" 2>/dev/null)
       log "⚠️ $CODE (시도 $i) $MSG"; sleep "$INTERVAL" ;;
  esac
done
log "⏸️ $((INTERVAL*MAX/60))분간 유휴가 안 됨 — 메시지는 큐에 남아 있다"
exit 2
