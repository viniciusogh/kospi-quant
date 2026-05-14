"""스크립트와 같은 디렉토리의 .env 를 import 시 자동 로드. 의존성 없음."""
import os

_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_path):
    with open(_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
