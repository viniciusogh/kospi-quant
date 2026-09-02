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
PEER=term_bf5219d5-c750-4ae3-af13-2487dcb5f879
SPEC="${1:?spec 파일 경로 필요}"
TITLE="${2:-클로드 검토 요청}"
LOG=codex_review.log
INTERVAL=${CODEX_RETRY_SEC:-45}
MAX=${CODEX_RETRY_MAX:-40}          # 45s × 40 = 30분
WAIT_SEC=${CODEX_WAIT_SEC:-20}      # 회신 파일 폴링 간격
WAIT_MAX=${CODEX_WAIT_MAX:-60}      # 20s × 60 = 20분

log(){ echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

OUT=codex_review_out.md
rm -f "$OUT"

# 회신 규약: 메시지 버스가 아니라 레포 파일로 받는다.
# 왜: 2026-09-02 검토는 dispatch 가 completed 로 끝났는데 내 수신함은 0건이었다.
# 코덱스가 답을 자기 화면에만 남기면 자동화가 반쪽이다. 파일은 내가 직접 확인할 수 있고,
# 레포 안 쓰기는 코덱스 기본 샌드박스로 이미 허용돼 권한 변경도 필요 없다.
SPEC_FULL=$(mktemp)
{
  cat "$SPEC"
  printf '\n\n---\n회신 방법 (반드시 지킬 것)\n'
  printf '결과를 %s/%s 에 써라. 화면에만 답하지 마라.\n' "$PWD" "$OUT"
  printf '다 썼으면 파일 마지막 줄에 === END === 를 남겨라. 그게 완료 신호다.\n'
} > "$SPEC_FULL"

TASK=$(orca orchestration task-create --from "$ME" --task-title "$TITLE" \
  --spec "$(cat "$SPEC_FULL")" --json 2>/dev/null \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['task']['id'])" 2>/dev/null)
[ -z "${TASK:-}" ] && { log "❌ task 생성 실패"; exit 1; }
log "task $TASK 생성 · 유휴 대기 시작 (최대 $((INTERVAL*MAX/60))분)"

for i in $(seq 1 "$MAX"); do
  CODE=$(orca orchestration dispatch --task "$TASK" --to "$PEER" --from "$ME" --inject --json 2>/dev/null \
    | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    print('OK' if d.get('ok') else d.get('error',{}).get('code','?'))
except Exception: print('parse')" 2>/dev/null)
  case "$CODE" in
    OK) log "✅ 주입 성공 (시도 $i) — 회신 파일 대기"
        for j in $(seq 1 "$WAIT_MAX"); do
          if [ -f "$OUT" ] && grep -q "=== END ===" "$OUT" 2>/dev/null; then
            log "📥 회신 도착 ($(wc -l < "$OUT" | tr -d ' ')줄)"
            rm -f "$SPEC_FULL"; exit 0
          fi
          sleep "$WAIT_SEC"
        done
        log "⏰ 회신 없음 ($((WAIT_SEC*WAIT_MAX/60))분) — 코덱스 창이 권한 프롬프트에서 멈췄는지 확인"
        rm -f "$SPEC_FULL"; exit 3 ;;
    agent_prompt_blocked) sleep "$INTERVAL" ;;         # 작업 중 — 조용히 기다린다
    *) log "⚠️ $CODE (시도 $i)"; sleep "$INTERVAL" ;;
  esac
done
log "⏸️ $((INTERVAL*MAX/60))분간 유휴가 안 됨 — 메시지는 큐에 남아 있다"
exit 2
