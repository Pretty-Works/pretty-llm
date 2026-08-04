# app/utils/logger.py
"""공용 로거.

에이전트 실행 흐름은 사람이 눈으로 따라가야 하는 경우가 많아
`[app.engine_b.worker.priority] ...` 처럼 컴포넌트 이름이 앞에 붙도록 통일한다.
"""

import logging
import sys
from functools import lru_cache

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATEFMT = "%H:%M:%S"


@lru_cache(maxsize=1)
def _configure_root() -> None:
    from app.config import get_settings

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))

    root = logging.getLogger("app")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(get_settings().log_level.upper())
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    """`get_logger("engine_b.validator")` -> `app.engine_b.validator` 로거."""
    _configure_root()
    return logging.getLogger(f"app.{name}")
