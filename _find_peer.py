"""Run 수신함에서 코덱스 핸들을 찾는다. 세션을 새로 띄우면 핸들이 바뀌므로
하드코딩하면 dispatch 가 조용히 실패한다 (2026-09-02)."""
import json, os, sys

me = os.environ.get("ME", "")
ms = (json.load(sys.stdin).get("result") or {}).get("messages") or []
# 나 아닌 발신자 중 가장 최근 것 = 현재 살아 있는 피어
peers = [m for m in ms if m.get("from_handle") and m["from_handle"] != me]
peers.sort(key=lambda m: str(m.get("created_at", "")), reverse=True)
print(peers[0]["from_handle"] if peers else "")
