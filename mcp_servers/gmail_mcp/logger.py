# mcp_servers/gmail_mcp/logger.py
"""app/utils/logger.py 와 같은 패턴. 별도 프로세스라 app.* 로거를 그대로 못 쓴다.

콜백 성공/실패 사유를 터미널에 바로 찍어서, 브라우저가 localhost:3000 으로
리다이렉트되며 ERR_CONNECTION_REFUSED 가 뜨는 것과 무관하게(그건 프론트가 없어서
나는 정상 현상) 실제 성공/실패는 이 로그로 구분하라고 만들었다.
"""

import logging
import sys
from functools import lru_cache

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATEFMT = "%H:%M:%S"


@lru_cache(maxsize=1)
def _configure_root() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))

    root = logging.getLogger("gmail_mcp")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    _configure_root()
    return logging.getLogger(f"gmail_mcp.{name}")
