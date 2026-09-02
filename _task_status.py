"""task id 의 현재 status 를 출력. codex_review.sh 가 죽은 작업 감지에 쓴다."""
import json, sys

want = sys.argv[1]
ts = (json.load(sys.stdin).get("result") or {}).get("tasks") or []
print(next((t.get("status", "") for t in ts if t.get("id") == want), ""))
