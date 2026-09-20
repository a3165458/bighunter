#!/bin/bash
# 快速诊断 CDP + Arkham transfers API 是否可用。
set -u

CDP="${ARKHAM_CDP_URL:-http://127.0.0.1:9222}/json/version"
HEALTH="${BOT_HEALTH:-http://127.0.0.1:8000/health}"

echo "== CDP =="
if curl -sf --max-time 3 "$CDP" >/dev/null; then
  echo "ok: $CDP"
else
  echo "FAIL: Chrome CDP not reachable at $CDP"
  echo "  → systemctl status arkham-chrome"
  exit 1
fi

echo "== Bot health =="
curl -sf --max-time 5 "$HEALTH" | python3 -m json.tool 2>/dev/null || echo "(bot down)"

echo "== Proxy (optional) =="
PROXY="${ARKHAM_PROXY_SERVER:-}"
if [[ -z "$PROXY" ]]; then
  echo "ARKHAM_PROXY_SERVER unset (direct egress)"
else
  echo "testing $PROXY ..."
  code=$(curl -s --max-time 8 --proxy "$PROXY" -o /dev/null -w '%{http_code}' https://arkm.com/ || echo 000)
  echo "arkm.com via proxy: HTTP $code"
  if [[ "$code" == "000" ]]; then
    echo "WARN: proxy unreachable — fix xray/VLESS or clear ARKHAM_PROXY_SERVER"
  fi
fi

echo "== Playwright transfers probe =="
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
python3 <<'PY'
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path('.').resolve()))

async def main():
    from playwright.async_api import async_playwright
    from app.config import settings
    import json

    cdp = (settings.arkham_cdp_url or 'http://127.0.0.1:9222').rstrip('/')
    storage = Path(settings.arkham_storage_state_path or 'data/arkham-storage-state.json')
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp, timeout=15000)
        ctx = browser.contexts[0]
        if storage.is_file():
            cookies = json.loads(storage.read_text()).get('cookies') or []
            if cookies:
                await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        state = {"ok": False, "failed": False}

        def on_response(r):
            if "api.arkm.com/transfers" in r.url and r.status == 200:
                state["ok"] = True

        def on_failed(r):
            if "api.arkm.com/transfers" in r.url:
                state["failed"] = True

        page.on("response", on_response)
        page.on("requestfailed", on_failed)
        await page.goto(settings.arkham_transfers_url or 'https://arkm.com/transfers', wait_until='domcontentloaded', timeout=90000)
        await asyncio.sleep(10)
        title = await page.title()
        await browser.close()
    if state["ok"]:
        print('PASS: transfers API returned 200')
        return 0
    if state["failed"]:
        print('FAIL: api.arkm.com/transfers net::ERR_FAILED (datacenter IP blocked)')
        print('  → restore working VLESS/residential proxy on Chrome, or set ARKHAM_API_KEY')
        return 2
    print(f'WARN: no transfers XHR observed (title={title!r})')
    return 1

raise SystemExit(asyncio.run(main()))
PY
