"""轮询心跳与 CDP 假活检测。"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from app import poller_health
from app.arkham_home_polling import cdp_session_is_dead


def test_cdp_session_is_dead_detects_hang_and_disconnect() -> None:
    assert cdp_session_is_dead(TimeoutError("BrowserType.connect_over_cdp: Timeout 180000ms exceeded."))
    assert cdp_session_is_dead(RuntimeError("Target closed"))
    assert cdp_session_is_dead(RuntimeError("Browser has been closed"))
    assert not cdp_session_is_dead(RuntimeError("Cloudflare not cleared this cycle"))


def test_heartbeat_marks_success_and_becomes_stale(tmp_path, monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "arkham_storage_state_path", str(tmp_path / "state.json"))
    poller_health.reset()
    now = time.time()
    poller_health.mark_success("arkham_home", 25, now=now)
    snap = poller_health.snapshot(stale_after=600)
    assert snap["ok"] is True
    assert snap["extracted"] == 25
    path = tmp_path / "arkham-heartbeat.json"
    assert json.loads(path.read_text())["extracted"] == 25

    poller_health.mark_success("arkham_home", 1, now=now - 700)
    snap = poller_health.snapshot(stale_after=600)
    assert snap["ok"] is False
    assert snap["age_seconds"] >= 700


def test_watchdog_dry_run_restarts_on_stale_heartbeat(tmp_path) -> None:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread

    class _Ok(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *_args) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), _Ok)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        heartbeat = tmp_path / "arkham-heartbeat.json"
        heartbeat.write_text(json.dumps({"ts": int(time.time()) - 900, "extracted": 1}))
        script = Path(__file__).resolve().parents[1] / "scripts" / "arkham_cdp_watchdog.sh"
        env = os.environ.copy()
        dummy = f"http://127.0.0.1:{port}/"
        env.update({
            "DRY_RUN": "1",
            "HEARTBEAT": str(heartbeat),
            "STALE_SEC": "60",
            "BOT_GRACE_SEC": "0",
            "RESTART_COOLDOWN": "0",
            "STATE_DIR": str(tmp_path / "run"),
            "BOT_HEALTH": dummy,
            "CDP_VERSION": dummy,
            "CONTAINER": "watchdog-test-missing",
        })
        result = subprocess.run(
            ["bash", str(script)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "action=restart-chrome" in result.stdout
        assert "heartbeat-stale" in result.stdout
    finally:
        server.shutdown()
