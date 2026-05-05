#!/usr/bin/env bash
# setup.sh — Deploy ArtImageHub inference server on Mac (192.168.68.221)
# Usage: bash setup.sh
# Run as user zj-db0812. Installs NAFNet + SwinIR + model weights + LaunchD service.
set -euo pipefail

ROOT="$HOME/inference-server"
MODELS="$ROOT/models"
VENV="$ROOT/venv"
LOG="$ROOT/server.log"
ERRLOG="$ROOT/server.err.log"
SERVER_PY="$ROOT/server.py"
PLIST_ID="com.artimagehub.inference"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_ID}.plist"
PORT=8765

echo "=== ArtImageHub inference server setup ==="
echo "ROOT: $ROOT"
echo "Device: $(uname -m)"

# ── 1. Directory structure ─────────────────────────────────────────────────────
mkdir -p "$ROOT" "$MODELS"

# ── 2. Copy server.py ─────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp "$SCRIPT_DIR/server.py" "$SERVER_PY"
echo "✓ server.py → $SERVER_PY"

# ── 3. Python virtual environment ─────────────────────────────────────────────
if [ ! -d "$VENV" ]; then
    python3 -m venv "$VENV"
    echo "✓ created venv at $VENV"
fi
source "$VENV/bin/activate"

pip install --upgrade pip --quiet
pip install --quiet \
    "torch>=2.1" torchvision \
    fastapi "uvicorn[standard]" \
    pillow numpy \
    basicsr \
    huggingface_hub
echo "✓ Python packages installed"

# ── 4. Clone NAFNet ───────────────────────────────────────────────────────────
if [ ! -d "$ROOT/NAFNet" ]; then
    git clone --depth 1 https://github.com/megvii-research/NAFNet.git "$ROOT/NAFNet"
    echo "✓ NAFNet cloned"
else
    echo "✓ NAFNet already present"
fi

# ── 5. Clone SwinIR ───────────────────────────────────────────────────────────
if [ ! -d "$ROOT/SwinIR" ]; then
    git clone --depth 1 https://github.com/JingyunLiang/SwinIR.git "$ROOT/SwinIR"
    echo "✓ SwinIR cloned"
else
    echo "✓ SwinIR already present"
fi

# ── 6. Download model weights ─────────────────────────────────────────────────
cd "$MODELS"

# SwinIR JPEG artifact removal (color, quality=40) — GitHub releases, ~35MB
if [ ! -f "006_CAR_DFWB_s126w7_SwinIR-M_jpeg40.pth" ]; then
    echo "Downloading SwinIR JPEG model..."
    curl -fL \
        "https://github.com/JingyunLiang/SwinIR/releases/download/v0.0/006_CAR_DFWB_s126w7_SwinIR-M_jpeg40.pth" \
        -o "006_CAR_DFWB_s126w7_SwinIR-M_jpeg40.pth"
    echo "✓ SwinIR JPEG model downloaded"
else
    echo "✓ SwinIR JPEG model already present"
fi

# NAFNet weights — try HuggingFace Hub (space artifacts), fall back to gdown
download_nafnet() {
    local NAME="$1"
    local GDRIVE_ID="$2"
    if [ -f "$NAME" ]; then
        echo "✓ $NAME already present"
        return
    fi
    echo "Downloading $NAME via HuggingFace Hub..."
    python3 -c "
import sys
try:
    from huggingface_hub import hf_hub_download
    import shutil, os
    # Try downloading from the NAFNet space (model files may be in the repo)
    path = hf_hub_download(
        repo_id='chuxiaojie/NAFNet',
        filename='experiments/pretrained_models/$NAME',
        repo_type='space',
        local_dir='$MODELS',
    )
    shutil.copy(path, '$MODELS/$NAME')
    print('Downloaded via HuggingFace Hub')
    sys.exit(0)
except Exception as e:
    print(f'HF Hub failed: {e}', file=sys.stderr)
    sys.exit(1)
" 2>/dev/null || {
        echo "HF Hub failed, trying gdown (Google Drive)..."
        pip install --quiet gdown
        python3 -c "
import gdown
gdown.download(id='$GDRIVE_ID', output='$MODELS/$NAME', quiet=False)
" || {
            echo "⚠ Could not download $NAME automatically."
            echo "  Manual download: https://github.com/megvii-research/NAFNet"
            echo "  Save to: $MODELS/$NAME"
        }
    }
}

# Google Drive IDs from NAFNet GitHub README
download_nafnet "NAFNet-SIDD-width64.pth" "14D4V4raNYIOhETfcuuLI3bGLB-OYIv6X"
download_nafnet "NAFNet-GoPro-width64.pth" "1S0PVlbyH89aK8a6-Sr6DNvMmKwVADHlz"

# ── 7. Auth token ─────────────────────────────────────────────────────────────
TOKEN_FILE="$HOME/.lama-server-token"
if [ ! -f "$TOKEN_FILE" ]; then
    # Generate a random token if not already set by lama_server setup
    python3 -c "import secrets; print(secrets.token_urlsafe(32))" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
    echo "✓ Generated auth token → $TOKEN_FILE"
    echo "  Token: $(cat "$TOKEN_FILE")"
    echo "  → Set LAMA_INFERENCE_TOKEN env var on Render to this value"
else
    echo "✓ Auth token already exists at $TOKEN_FILE ($(cat "$TOKEN_FILE" | cut -c1-8)...)"
fi

# ── 8. LaunchAgent (auto-start on login) ──────────────────────────────────────
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_ID}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${VENV}/bin/python</string>
        <string>${SERVER_PY}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>INFERENCE_ROOT</key>
        <string>${ROOT}</string>
        <key>PORT</key>
        <string>${PORT}</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>${ROOT}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${LOG}</string>
    <key>StandardErrorPath</key>
    <string>${ERRLOG}</string>
    <key>ThrottleInterval</key>
    <integer>10</integer>
</dict>
</plist>
PLIST

# Load / reload the service
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load -w "$PLIST_PATH"
echo "✓ LaunchAgent registered: $PLIST_ID (auto-starts on login, port $PORT)"

# ── 9. Quick health check ──────────────────────────────────────────────────────
echo ""
echo "Waiting 5s for server to start..."
sleep 5
if curl -sf "http://localhost:$PORT/health" >/dev/null; then
    echo "✓ Server is running: http://localhost:$PORT/health"
    curl -s "http://localhost:$PORT/health" | python3 -m json.tool
else
    echo "⚠ Server did not respond yet — check logs: $LOG / $ERRLOG"
fi

# ── 10. Cloudflare Tunnel reminder ────────────────────────────────────────────
echo ""
echo "=== Next step: Cloudflare Tunnel ==="
echo "This exposes http://localhost:$PORT to Render's backend."
echo ""
echo "Option A — Quick (ephemeral, resets on restart):"
echo "  brew install cloudflare/cloudflare/cloudflared"
echo "  cloudflared tunnel --url http://localhost:$PORT"
echo "  # Copy the https://*.trycloudflare.com URL → Render LAMA_INFERENCE_URL"
echo ""
echo "Option B — Persistent named tunnel:"
echo "  cloudflared tunnel login"
echo "  cloudflared tunnel create artimagehub-inference"
echo "  cloudflared tunnel route dns artimagehub-inference inference.artimagehub.com"
echo "  cloudflared tunnel run artimagehub-inference"
echo "  # Then set LAMA_INFERENCE_URL=https://inference.artimagehub.com on Render"
echo ""
echo "Also set on Render:"
echo "  LAMA_INFERENCE_TOKEN=$(cat "$HOME/.lama-server-token")"
echo ""
echo "=== Setup complete ==="
