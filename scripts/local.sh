#!/usr/bin/env bash
# Run Jev Ultrafast with no TypeSafe key: a local laya-browser decision server plus Chrome for Testing.
#
#   scripts/local.sh            # decision server + Chrome for Testing + the inspector at http://127.0.0.1:8766
#   scripts/local.sh --headless # same, Chrome without a window (fine unless you need to sign in to a site)
#
# Text the agent types still comes from the OpenAI-compatible model in .env (TEXT_MODEL_*).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
MODEL_DIR="${LAYA_BROWSER_DIR:-$ROOT/.laya-browser}"
PORT="${LAYA_BROWSER_PORT:-8791}"
CDP_PORT="${JEV_CDP_PORT:-9333}"
PROFILE="${JEV_CHROME_PROFILE:-$HOME/.jev-chrome-for-testing}"
HEADLESS=()
[ "${1:-}" = "--headless" ] && HEADLESS=(--headless=new)

# 1. The fine-tuned decision model (cklxx/laya-browser, Apache-2.0, ~650 MB) and its server.
if [ ! -f "$MODEL_DIR/model.safetensors" ]; then
  uv run --project local hf download cklxx/laya-browser --local-dir "$MODEL_DIR" --exclude "assets/*" "results/*" "code/*"
fi
uv run --project local python "$MODEL_DIR/laya_browser.py" serve --model "$MODEL_DIR" --port "$PORT" &
SERVER=$!

# 2. Chrome for Testing only, never the branded Chrome app (Playwright's copy: npx playwright install chromium).
CHROME="${CHROME_FOR_TESTING:-$(ls -d "$HOME"/Library/Caches/ms-playwright/chromium-*/chrome-mac*/"Google Chrome for Testing.app" 2>/dev/null | sort -V | tail -1)/Contents/MacOS/Google Chrome for Testing}"
[ -x "$CHROME" ] || { echo "Chrome for Testing not found; run: npx playwright install chromium" >&2; kill $SERVER; exit 1; }
"$CHROME" "${HEADLESS[@]}" --remote-debugging-port="$CDP_PORT" --user-data-dir="$PROFILE" --no-first-run \
  --no-default-browser-check about:blank >/dev/null 2>&1 &
CHROME_PID=$!
trap 'kill $SERVER $CHROME_PID 2>/dev/null' EXIT

until curl -s "http://127.0.0.1:$CDP_PORT/json/version" >/dev/null; do sleep 1; done
until curl -s -o /dev/null "http://127.0.0.1:$PORT/v1/systemone" -d '{}'; do sleep 2; done

# 3. Jev against the local server. Browser Harness telemetry stays off.
export TYPESAFE_BASE_URL="http://127.0.0.1:$PORT" TYPESAFE_API_KEY="${TYPESAFE_API_KEY:-local}"
export BU_CDP_URL="http://127.0.0.1:$CDP_PORT" BH_TELEMETRY=0 JEV_CDP_TIMEOUT="${JEV_CDP_TIMEOUT:-30}"
uv run --env-file .env jev
