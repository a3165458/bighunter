import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolate_token_guard(tmp_path, monkeypatch):
    """每个测试使用独立屏蔽状态，避免读写仓库里的 data/。"""
    from app.config import settings
    from app import token_guard

    path = tmp_path / "token_guard.json"
    monkeypatch.setattr(settings, "token_guard_path", str(path))
    monkeypatch.setattr(settings, "blocked_tokens", "")
    monkeypatch.setattr(
        settings,
        "arkham_storage_state_path",
        str(tmp_path / "arkham-storage-state.json"),
    )
    token_guard.reset()
    from app import poller_health
    poller_health.reset()
    yield
    token_guard.reset()
    poller_health.reset()
