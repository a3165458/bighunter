"""轮询心跳：/health 展示 + 宿主 watchdog 判断是否该重启 Chrome。"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger("whalescope.poller_health")

_lock = threading.Lock()
_last_success_at: float = 0.0
_last_error_at: float = 0.0
_last_error: str = ""
_last_source: str = ""
_last_extracted: int = 0


def heartbeat_path() -> Path:
    parent = Path(
        settings.arkham_storage_state_path or "data/arkham-storage-state.json"
    ).parent
    return parent / "arkham-heartbeat.json"


def reset() -> None:
    global _last_success_at, _last_error_at, _last_error, _last_source, _last_extracted
    with _lock:
        _last_success_at = 0.0
        _last_error_at = 0.0
        _last_error = ""
        _last_source = ""
        _last_extracted = 0


def mark_success(source: str, extracted: int = 0, now: Optional[float] = None) -> None:
    global _last_success_at, _last_source, _last_extracted
    now = time.time() if now is None else now
    with _lock:
        _last_success_at = now
        _last_source = source
        _last_extracted = int(extracted)
        payload = {
            "ts": int(now),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "source": source,
            "extracted": int(extracted),
        }
    path = heartbeat_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("Failed to write poller heartbeat %s: %s", path, exc)


def mark_error(source: str, error: str, now: Optional[float] = None) -> None:
    global _last_error_at, _last_error, _last_source
    now = time.time() if now is None else now
    with _lock:
        _last_error_at = now
        _last_error = str(error)[:300]
        _last_source = source


def snapshot(stale_after: int = 600) -> dict[str, Any]:
    now = time.time()
    with _lock:
        success_at = _last_success_at
        error_at = _last_error_at
        error = _last_error
        source = _last_source
        extracted = _last_extracted
    age = (now - success_at) if success_at else None
    ok = success_at > 0 and (now - success_at) <= stale_after
    return {
        "ok": ok,
        "source": source,
        "extracted": extracted,
        "last_success_at": (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(success_at))
            if success_at
            else None
        ),
        "age_seconds": None if age is None else int(age),
        "stale_after_seconds": stale_after,
        "last_error": error or None,
        "last_error_at": (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(error_at))
            if error_at
            else None
        ),
        "heartbeat_path": str(heartbeat_path()),
    }
