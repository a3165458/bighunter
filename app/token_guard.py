"""代币屏蔽、同币冷却、妖币热度过滤。

同类开源方案（crypto-radar / yaobi-radar）用静态黑名单 + 频次抑制
把「反复刷屏的币」从候选里拿掉。这里落到 Arkham 大额转账管线：
同一代币短时间多次过阈值不是妖币，而是噪音。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger("whalescope.token_guard")

_lock = threading.Lock()
_state: Optional[dict[str, Any]] = None


def _empty_state() -> dict[str, Any]:
    return {"blocked": {}, "history": {}}


def normalize_token(token: str) -> str:
    return (token or "").strip().upper()


def _parse_csv_tokens(raw: str) -> set[str]:
    return {normalize_token(part) for part in (raw or "").split(",") if normalize_token(part)}


def env_blocked_tokens() -> set[str]:
    return _parse_csv_tokens(settings.blocked_tokens)


def admin_user_ids() -> set[str]:
    return {part.strip() for part in (settings.telegram_admin_user_ids or "").split(",") if part.strip()}


def _path() -> Path:
    return Path(settings.token_guard_path)


def reset() -> None:
    """测试用：丢弃内存缓存，下次从磁盘重读。"""
    global _state
    with _lock:
        _state = None


def _load_unlocked() -> dict[str, Any]:
    global _state
    if _state is not None:
        return _state
    path = _path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _state = _empty_state()
        return _state
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load token guard state %s: %s", path, exc)
        _state = _empty_state()
        return _state
    if not isinstance(raw, dict):
        _state = _empty_state()
        return _state
    blocked = raw.get("blocked") if isinstance(raw.get("blocked"), dict) else {}
    history = raw.get("history") if isinstance(raw.get("history"), dict) else {}
    _state = {
        "blocked": {normalize_token(k): v for k, v in blocked.items() if normalize_token(k)},
        "history": {normalize_token(k): v for k, v in history.items() if normalize_token(k)},
    }
    return _state


def _save_unlocked(state: dict[str, Any]) -> None:
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("Failed to persist token guard state %s: %s", path, exc)


def _prune_history(history: list[float], now: float) -> list[float]:
    window = max(int(settings.token_alert_window_seconds), 0)
    keep_after = now - max(window * 2, 86_400)
    pruned = [float(ts) for ts in history if isinstance(ts, (int, float)) and ts >= keep_after]
    return pruned[-50:]


def _block_entry(token: str, state: dict[str, Any]) -> Optional[dict[str, Any]]:
    entry = state["blocked"].get(token)
    return entry if isinstance(entry, dict) else None


def is_user_allowed(user_id: object) -> bool:
    allowed = admin_user_ids()
    if not allowed:
        return True
    return str(user_id or "") in allowed


def block_token(
    token: str,
    *,
    reason: str = "manual",
    source: str = "api",
    until: float = 0.0,
) -> str:
    token = normalize_token(token)
    if not token:
        raise ValueError("empty token")
    now = time.time()
    with _lock:
        state = _load_unlocked()
        state["blocked"][token] = {
            "blocked_at": now,
            "until": float(until or 0.0),
            "reason": reason,
            "source": source,
        }
        _save_unlocked(state)
    if until and until > now:
        logger.info("Snoozed %s until %.0f (%s/%s)", token, until, reason, source)
    else:
        logger.info("Blocked %s (%s/%s)", token, reason, source)
    return token


def unblock_token(token: str) -> bool:
    token = normalize_token(token)
    if not token:
        return False
    with _lock:
        state = _load_unlocked()
        existed = token in state["blocked"]
        state["blocked"].pop(token, None)
        if existed:
            _save_unlocked(state)
    if existed:
        logger.info("Unblocked %s", token)
    return existed


def list_blocked(now: Optional[float] = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for token in sorted(env_blocked_tokens()):
        items.append({
            "token": token,
            "until": 0.0,
            "reason": "env",
            "source": "BLOCKED_TOKENS",
            "permanent": True,
        })
        seen.add(token)
    with _lock:
        state = _load_unlocked()
        blocked = dict(state["blocked"])
    for token, entry in sorted(blocked.items()):
        if token in seen:
            continue
        until = float(entry.get("until") or 0.0)
        if until and until <= now:
            continue
        items.append({
            "token": token,
            "until": until,
            "reason": entry.get("reason") or "manual",
            "source": entry.get("source") or "runtime",
            "permanent": until <= 0,
        })
    return items


def recent_alert_count(token: str, now: Optional[float] = None) -> int:
    token = normalize_token(token)
    now = time.time() if now is None else now
    window = max(int(settings.token_alert_window_seconds), 0)
    if not token or window <= 0:
        return 0
    with _lock:
        state = _load_unlocked()
        history = state["history"].get(token) or []
    return sum(1 for ts in history if isinstance(ts, (int, float)) and now - float(ts) < window)


def last_alert_at(token: str) -> float:
    token = normalize_token(token)
    with _lock:
        state = _load_unlocked()
        history = state["history"].get(token) or []
    stamps = [float(ts) for ts in history if isinstance(ts, (int, float))]
    return max(stamps) if stamps else 0.0


def record_alert(token: str, now: Optional[float] = None) -> None:
    token = normalize_token(token)
    if not token:
        return
    now = time.time() if now is None else now
    with _lock:
        state = _load_unlocked()
        history = list(state["history"].get(token) or [])
        history.append(now)
        state["history"][token] = _prune_history(history, now)
        _save_unlocked(state)


def _can_break_limits(usd_value: float) -> bool:
    break_usd = float(settings.token_cooldown_break_usd or 0.0)
    return break_usd > 0 and usd_value >= break_usd


def evaluate(token: str, usd_value: float, now: Optional[float] = None) -> tuple[bool, str]:
    """返回 (True, "") 表示放行；(False, reason) 表示应跳过。"""
    token = normalize_token(token)
    now = time.time() if now is None else now
    if not token:
        return True, ""

    if token in env_blocked_tokens():
        return False, "blocked_token"

    with _lock:
        state = _load_unlocked()
        entry = _block_entry(token, state)
        history = list(state["history"].get(token) or [])
        if entry and float(entry.get("until") or 0.0) and float(entry["until"]) <= now:
            state["blocked"].pop(token, None)
            _save_unlocked(state)
            entry = None

    if entry is not None:
        until = float(entry.get("until") or 0.0)
        if until > now:
            return False, "token_snoozed"
        return False, "blocked_token"

    if _can_break_limits(usd_value):
        return True, ""

    cooldown = max(int(settings.token_cooldown_seconds), 0)
    if cooldown > 0:
        last = max((float(ts) for ts in history if isinstance(ts, (int, float))), default=0.0)
        if last and now - last < cooldown:
            return False, "token_cooldown"

    if settings.yaobi_mode:
        window = max(int(settings.token_alert_window_seconds), 0)
        limit = max(int(settings.token_max_alerts_per_window), 0)
        if window > 0 and limit > 0:
            count = sum(1 for ts in history if isinstance(ts, (int, float)) and now - float(ts) < window)
            if count >= limit:
                return False, "token_heat"

    return True, ""


def describe_heat(token: str, now: Optional[float] = None) -> str:
    """给消息用的热度说明：本条算进去之后是窗口内第几次。"""
    if not settings.yaobi_mode:
        return ""
    window = max(int(settings.token_alert_window_seconds), 0)
    if window <= 0:
        return ""
    count = recent_alert_count(token, now=now) + 1
    hours = max(window // 3600, 1)
    if count <= 1:
        return f"🆕 {hours}h 窗口内首次出现"
    limit = max(int(settings.token_max_alerts_per_window), 0)
    suffix = f"，上限 {limit} 次" if limit else ""
    return f"🔁 {hours}h 窗口内第 {count} 次{suffix}"


def mute_keyboard(token: str) -> dict[str, Any]:
    token = normalize_token(token) or "UNKNOWN"
    return {
        "inline_keyboard": [[
            {"text": f"🔇 屏蔽 {token}", "callback_data": f"mute|{token}"[:64]},
            {"text": "⏱ 静音 6h", "callback_data": f"snooze|{token}|21600"[:64]},
        ]]
    }


def snapshot() -> dict[str, Any]:
    blocked = list_blocked()
    with _lock:
        state = _load_unlocked()
        tracked = len(state["history"])
    return {
        "yaobi_mode": settings.yaobi_mode,
        "cooldown_seconds": settings.token_cooldown_seconds,
        "max_alerts_per_window": settings.token_max_alerts_per_window,
        "alert_window_seconds": settings.token_alert_window_seconds,
        "blocked_count": len(blocked),
        "blocked": [item["token"] for item in blocked],
        "tracked_tokens": tracked,
    }
