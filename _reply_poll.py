"""코덱스 회신을 Run 수신함에서 찾는다. codex_review.sh 가 호출."""
import json, os, sys

peer = os.environ["PEER"][-12:]
since = os.environ["SINCE"]
ms = (json.load(sys.stdin).get("result") or {}).get("messages") or []
hit = [m for m in ms
       if str(m.get("from_handle", "")).endswith(peer)
       and str(m.get("created_at", "")) >= since
       and m.get("type") in ("status", "worker_done")]
hit.sort(key=lambda m: m["created_at"])
print("\n\n".join(f"[{m['type']}] {m.get('subject','')}\n{m.get('body','')}" for m in hit))
