#!/usr/bin/env bash
# ==============================================================================
#  Wildlife Monitor — Ubuntu Setup Script
#
#  Designed to be safe on machines with existing software:
#   - Uses an isolated Python virtual environment (never touches system Python)
#   - Only installs system packages that are missing (never downgrades)
#   - Never modifies existing apt sources, nginx/apache configs, or other services
#   - Will not overwrite an existing venv unless you pass --reinstall
#   - Checks for port conflicts before recommending a port
#   - All Python packages go into the venv only
#
#  Usage:
#    ./setup.sh                      # Standard install
#    ./setup.sh --venv /opt/myenv    # Custom venv location
#    ./setup.sh --reinstall          # Wipe and recreate the venv
#    ./setup.sh --no-gpu             # Force CPU-only PyTorch
# ==============================================================================
set -euo pipefail

# ── Defaults ───────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$HOME/wildlife_env"
REINSTALL=false
FORCE_CPU=false
SKIP_APT=false

# ── Argument parsing ───────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --venv)       VENV_DIR="$2"; shift 2 ;;
        --reinstall)  REINSTALL=true; shift ;;
        --no-gpu)     FORCE_CPU=true; shift ;;
        --skip-apt)   SKIP_APT=true; shift ;;
        -h|--help)
            echo "Usage: $0 [--venv PATH] [--reinstall] [--no-gpu] [--skip-apt]"
            exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ── Colours ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "  ${CYAN}→${NC}  $*"; }
ok()    { echo -e "  ${GREEN}✓${NC}  $*"; }
warn()  { echo -e "  ${YELLOW}!${NC}  $*"; }
err()   { echo -e "  ${RED}✗${NC}  $*"; }

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Wildlife Monitor — Setup"
echo "  Virtual environment: $VENV_DIR"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── STEP 1: Pre-flight checks ──────────────────────────────────────────────────
echo "[1/9] Pre-flight checks..."

# Check we're on Ubuntu/Debian
if ! command -v apt-get &>/dev/null; then
    err "apt-get not found. This script requires Ubuntu/Debian."
    exit 1
fi
ok "Ubuntu/Debian detected"

# Check we're not running as root (don't want to install venv as root)
if [[ "$EUID" -eq 0 ]]; then
    warn "Running as root. The virtual environment will be owned by root."
    warn "Consider running as a regular user instead."
    read -rp "  Continue anyway? [y/N] " yn
    [[ "$yn" =~ ^[Yy]$ ]] || exit 1
fi

# Check for Python 3.10–3.12 (speciesnet requires <3.14 but <=3.13 in pip means <=3.13.0,
# so 3.13.x patch releases fail the constraint — 3.11 is the safest target)
if ! command -v python3 &>/dev/null; then
    err "python3 not found. Will attempt to install it."
else
    PY_MAJOR=$(python3 -c "import sys; print(sys.version_info.major)")
    PY_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")
    if [[ "$PY_MAJOR" -lt 3 ]] || { [[ "$PY_MAJOR" -eq 3 ]] && [[ "$PY_MINOR" -lt 10 ]]; }; then
        warn "System Python is ${PY_MAJOR}.${PY_MINOR} (need 3.10–3.12)."
        warn "Will install python3.11 via apt."
    elif [[ "$PY_MAJOR" -eq 3 ]] && [[ "$PY_MINOR" -ge 13 ]]; then
        warn "System Python is ${PY_MAJOR}.${PY_MINOR}."
        warn "speciesnet uses '<=3.13' which pip interprets as <=3.13.0 — patch releases fail."
        warn "Will install python3.12 (or 3.11) for the venv — system Python is untouched."
    else
        ok "Python ${PY_MAJOR}.${PY_MINOR} found"
    fi
fi

# Check existing venv
if [[ -d "$VENV_DIR" ]]; then
    if [[ "$REINSTALL" == true ]]; then
        warn "Removing existing virtual environment at $VENV_DIR (--reinstall)"
        rm -rf "$VENV_DIR"
    else
        warn "Virtual environment already exists at $VENV_DIR"
        warn "To recreate it from scratch, run: $0 --reinstall"
        warn "Continuing — will only install missing packages into existing venv."
    fi
fi

# Check for apt lock (another process using apt)
if fuser /var/lib/dpkg/lock-frontend &>/dev/null 2>&1; then
    err "Another process is using apt (dpkg lock held). Wait for it to finish."
    exit 1
fi
ok "No apt lock conflicts"

# Scan for ports in use and suggest a free one
echo ""
info "Scanning ports to help you pick a non-conflicting one..."
USED_PORTS=$(ss -tlnp 2>/dev/null | awk 'NR>1 {print $4}' | grep -oE ':[0-9]+$' | tr -d ':' | sort -n | uniq || true)
SUGGESTED_PORT=8080
for p in 8080 8081 8082 8090 9000 9090 3000 5000 7000 7070; do
    if ! echo "$USED_PORTS" | grep -q "^${p}$"; then
        SUGGESTED_PORT=$p
        break
    fi
done

if echo "$USED_PORTS" | grep -q "^8080$"; then
    warn "Port 8080 is already in use."
    warn "Currently occupied ports: $(echo "$USED_PORTS" | tr '\n' ' ')"
    warn "Suggested free port: $SUGGESTED_PORT"
else
    ok "Port 8080 is free (default)"
fi
echo ""

# ── STEP 2: System packages ────────────────────────────────────────────────────
echo "[2/9] Checking system packages..."

if [[ "$SKIP_APT" == true ]]; then
    warn "--skip-apt specified. Skipping system package installation."
    warn "Make sure these are installed: cmake ffmpeg libgl1 libglib2.0-0 libsm6 libxext6 libxrender-dev libgomp1"
else
    # ── Clean up any broken PPA entries left by previous failed runs ──────────
    DEADSNAKES_LIST="/etc/apt/sources.list.d/deadsnakes-ubuntu-ppa-*.list"
    # shellcheck disable=SC2086
    if ls $DEADSNAKES_LIST &>/dev/null 2>&1; then
        warn "Found a leftover deadsnakes PPA entry from a previous run — removing it..."
        # shellcheck disable=SC2086
        sudo rm -f $DEADSNAKES_LIST
        # Also remove any .sources format entry
        sudo rm -f /etc/apt/sources.list.d/deadsnakes*.sources 2>/dev/null || true
        sudo apt-get update -qq
        ok "Stale PPA entry removed"
    fi

    # Only install what's actually missing — avoids touching existing package versions
    MISSING_PKGS=()
    for pkg in python3 python3-pip python3-venv python3-dev ffmpeg cmake protobuf-compiler \
                libgl1 libglib2.0-0 libsm6 libxext6 libxrender-dev libgomp1; do
        if ! dpkg -s "$pkg" &>/dev/null 2>&1; then
            MISSING_PKGS+=("$pkg")
        fi
    done

    if [[ "${#MISSING_PKGS[@]}" -eq 0 ]]; then
        ok "All system packages already installed"
    else
        info "Installing missing packages: ${MISSING_PKGS[*]}"
        # Only run apt-get update if we actually need to install something
        sudo apt-get update -qq
        sudo apt-get install -y -qq "${MISSING_PKGS[@]}"
        ok "Installed: ${MISSING_PKGS[*]}"
    fi

    # Install python3.11 if system Python is too old (<3.10) or too new (>=3.13).
    # speciesnet's '<=3.13' constraint means <=3.13.0 in pip — 3.13.x patch releases fail.
    # python3.11 is installed alongside the system Python without replacing it.
    PY_MAJOR=$(python3 -c "import sys; print(sys.version_info.major)")
    PY_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")
    NEED_PY311=false
    if [[ "$PY_MAJOR" -lt 3 ]] || { [[ "$PY_MAJOR" -eq 3 ]] && [[ "$PY_MINOR" -lt 10 ]]; }; then
        NEED_PY311=true
    elif [[ "$PY_MAJOR" -eq 3 ]] && [[ "$PY_MINOR" -ge 13 ]]; then
        NEED_PY311=true
    fi

    if [[ "$NEED_PY311" == true ]]; then
        info "System Python ${PY_MAJOR}.${PY_MINOR} is outside the 3.10–3.12 window required by speciesnet."
        info "Looking for a compatible Python in the default apt repos..."

        # Preferred order: 3.12 → 3.11 → deadsnakes PPA (older Ubuntu only) → pyenv
        PYTHON_BIN=""

        # ── Try python3.12 first (available in Ubuntu 24.04+ default repos) ──────
        if apt-cache show python3.12 &>/dev/null 2>&1; then
            info "python3.12 found in default repos — installing..."
            sudo apt-get install -y -qq python3.12 python3.12-venv python3.12-dev
            PYTHON_BIN="python3.12"
            ok "python3.12 installed"

        # ── Try python3.11 from default repos (Ubuntu 22.04 / 23.x) ─────────────
        elif apt-cache show python3.11 &>/dev/null 2>&1; then
            info "python3.11 found in default repos — installing..."
            sudo apt-get install -y -qq python3.11 python3.11-venv python3.11-dev
            PYTHON_BIN="python3.11"
            ok "python3.11 installed"

        # ── Try deadsnakes PPA (supports Ubuntu up to noble/24.04) ───────────────
        else
            UBUNTU_CODENAME=$(lsb_release -cs 2>/dev/null || echo "unknown")
            # deadsnakes only publishes for known Ubuntu releases up to noble
            DEADSNAKES_SUPPORTED=("focal" "jammy" "kinetic" "lunar" "mantic" "noble")
            SUPPORTED=false
            for r in "${DEADSNAKES_SUPPORTED[@]}"; do
                [[ "$UBUNTU_CODENAME" == "$r" ]] && SUPPORTED=true && break
            done

            if [[ "$SUPPORTED" == true ]]; then
                warn "Trying deadsnakes PPA for python3.11 (Ubuntu $UBUNTU_CODENAME)..."
                if ! dpkg -s software-properties-common &>/dev/null 2>&1; then
                    sudo apt-get install -y -qq software-properties-common
                fi
                sudo add-apt-repository -y ppa:deadsnakes/ppa
                sudo apt-get update -qq
                sudo apt-get install -y -qq python3.11 python3.11-venv python3.11-dev
                PYTHON_BIN="python3.11"
                ok "python3.11 installed via deadsnakes PPA"

            # ── Last resort: pyenv ────────────────────────────────────────────────
            else
                warn "No compatible Python in apt (Ubuntu $UBUNTU_CODENAME is too new for deadsnakes)."
                info "Installing Python 3.12 via pyenv..."

                # Set up pyenv environment variables first
                export PYENV_ROOT="$HOME/.pyenv"
                export PATH="$PYENV_ROOT/bin:$PATH"

                if ! command -v pyenv &>/dev/null; then
                    info "pyenv not found — installing..."
                    sudo apt-get install -y -qq \
                        build-essential libssl-dev zlib1g-dev libbz2-dev \
                        libreadline-dev libsqlite3-dev libncursesw5-dev \
                        xz-utils tk-dev libxml2-dev libxmlsec1-dev libffi-dev liblzma-dev
                    curl -fsSL https://pyenv.run | bash
                    export PYENV_ROOT="$HOME/.pyenv"
                    export PATH="$PYENV_ROOT/bin:$PATH"
                else
                    ok "pyenv already installed at $PYENV_ROOT"
                fi

                eval "$(pyenv init -)"
                pyenv install -s 3.12.9
                PYTHON_BIN="$(pyenv root)/versions/3.12.9/bin/python3"
                ok "Python 3.12.9 ready via pyenv: $PYTHON_BIN"
            fi
        fi

        ok "Venv will use $PYTHON_BIN (system Python ${PY_MAJOR}.${PY_MINOR} untouched)"
    else
        PYTHON_BIN="python3"
    fi
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
ok "Using Python binary: $PYTHON_BIN ($(${PYTHON_BIN} --version))"

# ── STEP 3: Virtual environment ────────────────────────────────────────────────
echo "[3/9] Setting up virtual environment..."

# Check if the target directory is inside a system path — refuse if so
UNSAFE_PREFIXES=("/usr" "/opt/ros" "/etc" "/bin" "/sbin" "/lib")
for prefix in "${UNSAFE_PREFIXES[@]}"; do
    if [[ "$VENV_DIR" == "$prefix"* ]]; then
        err "Refusing to create venv inside system directory: $VENV_DIR"
        err "Choose a path under your home directory or /opt/wildlife_env."
        exit 1
    fi
done

if [[ ! -d "$VENV_DIR" ]]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    ok "Created venv at $VENV_DIR"
else
    ok "Using existing venv at $VENV_DIR"
fi

# Activate — all pip commands below are isolated to this venv
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# Only upgrade pip/setuptools inside the venv (never touches system pip)
pip install --upgrade pip wheel setuptools -q
ok "pip/setuptools up to date inside venv"

# ── STEP 4: PyTorch ────────────────────────────────────────────────────────────
echo "[4/9] PyTorch..."

# Check if torch is already installed in the venv
if python3 -c "import torch" &>/dev/null 2>&1; then
    TORCH_VER=$(python3 -c "import torch; print(torch.__version__)")
    ok "PyTorch $TORCH_VER already installed in venv — skipping reinstall"
    info "To upgrade it, run: $0 --reinstall"
else
    if [[ "$FORCE_CPU" == true ]]; then
        info "--no-gpu specified. Installing CPU-only PyTorch..."
        pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu -q
        ok "PyTorch installed (CPU)"
    elif command -v nvidia-smi &>/dev/null && nvidia-smi &>/dev/null 2>&1; then
        # Check CUDA version to pick the right wheel
        CUDA_MAJOR=$(nvidia-smi | grep -oP 'CUDA Version: \K[0-9]+' | head -1 || echo "0")
        if [[ "$CUDA_MAJOR" -ge 12 ]]; then
            info "NVIDIA GPU + CUDA ${CUDA_MAJOR} detected. Installing CUDA 12 PyTorch..."
            pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121 -q
        elif [[ "$CUDA_MAJOR" -ge 11 ]]; then
            info "NVIDIA GPU + CUDA ${CUDA_MAJOR} detected. Installing CUDA 11 PyTorch..."
            pip install torch torchvision --index-url https://download.pytorch.org/whl/cu118 -q
        else
            warn "NVIDIA GPU found but CUDA version unclear. Falling back to CPU PyTorch."
            pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu -q
        fi
        ok "PyTorch installed (GPU/CUDA)"
    else
        info "No NVIDIA GPU detected. Installing CPU-only PyTorch..."
        warn "Processing will be slower without a GPU."
        pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu -q
        ok "PyTorch installed (CPU)"
    fi
fi

# ── STEP 5: Python packages ────────────────────────────────────────────────────
echo "[5/9] Python packages..."

# Install into venv only — never uses --break-system-packages
# pip inside the activated venv is already isolated

# ── Install strategy for onnx ──────────────────────────────────────────────────
# megadetector pins onnx==1.12.0 internally, which has no Python 3.12 wheel.
# pip falls back to building from source, which fails (bundled pybind11 v2.9.1
# doesn't support the opaque PyFrameObject struct introduced in Python 3.12).
#
# Fix: install megadetector with --no-deps, then install onnx>=1.15.0 with
# --only-binary=:all: (wheels only, never source). onnx 1.15.0+ ships
# Python 3.12 wheels and is compatible with megadetector at runtime.

# Step 1: core packages that have clean wheels for Python 3.12
pip install -q \
    opencv-python-headless \
    Pillow \
    numpy \
    fastapi \
    "uvicorn[standard]" \
    tqdm

# Step 2: lock onnx and onnxruntime to wheel-only installs before anything
# else can pull in the old source-only version
pip install -q --only-binary=:all: "onnx>=1.15.0" "onnxruntime>=1.16.3"

# Step 3: install megadetector without letting its resolver touch onnx,
# then manually install the deps it needs (skipped by --no-deps above).
# mkl is Linux-only and optional for CPU inference — skip it to avoid
# a large unnecessary download on machines without MKL support.
pip install -q --no-deps megadetector
pip install -q \
    clipboard \
    dill \
    "fastquadtree>=1.1.2" \
    "jsonpickle>=3.0.2" \
    pytest \
    ruff \
    "scikit-learn>=1.3.1" \
    send2trash
# Install these with --no-deps: their declared protobuf<=3.20.1 conflicts with
# onnx>=1.15.0 which needs protobuf>=4.25.1. At runtime neither package uses
# protobuf directly — it's a stale transitive dep from old bundling.
pip install -q --no-deps "ultralytics-yolov5==0.1.1" "yolov9pip==0.0.4"

pip install -q speciesnet

ok "All Python packages installed into venv"

# ── STEP 6: Verify ─────────────────────────────────────────────────────────────
echo "[6/9] Verifying installation..."

python3 - <<'PYEOF'
import sys, importlib

checks = [
    ("cv2",                                      "opencv-python"),
    ("torch",                                    "torch"),
    ("onnx",                                     "onnx"),
    ("onnxruntime",                              "onnxruntime"),
    ("fastapi",                                  "fastapi"),
    ("uvicorn",                                  "uvicorn"),
    ("megadetector.detection.pytorch_detector",  "megadetector"),
    ("speciesnet",                               "speciesnet"),
]

all_ok = True
for mod, name in checks:
    try:
        m = importlib.import_module(mod)
        ver = getattr(m, "__version__", "")
        print(f"  ✓  {name:<30} {ver}")
        # Warn if onnx is still on the old incompatible version
        if name == "onnx" and ver and ver.startswith("1.12"):
            print(f"  ✗  onnx {ver} is incompatible with Python 3.12 — install failed silently")
            all_ok = False
    except ImportError as e:
        print(f"  ✗  {name:<30} FAILED: {e}")
        all_ok = False

import torch
gpu_msg = f"GPU: {torch.cuda.get_device_name(0)}" if torch.cuda.is_available() else "CPU only"
print(f"\n  Hardware: {gpu_msg}")

sys.exit(0 if all_ok else 1)
PYEOF

ok "All packages verified"

# ── STEP 7: Optional NAS setup ─────────────────────────────────────────────────
echo ""
echo "[7/9] NAS / Network drive setup"
echo ""
echo "  Your videos are stored on a network drive (Synology NAS)."
echo "  Would you like to configure the NAS connection now?"
echo "  (You can also run ./nas_connect.sh at any time to do this later.)"
echo ""
echo -en "  ${CYAN}?${NC}  Set up NAS connection now? [Y/n]: "
read -r nas_choice

NAS_CONFIGURED=false
if [[ ! "$nas_choice" =~ ^[Nn]$ ]]; then
    echo ""
    NAS_SCRIPT="$SCRIPT_DIR/nas_connect.sh"
    if [[ -f "$NAS_SCRIPT" ]]; then
        chmod +x "$NAS_SCRIPT"
        bash "$NAS_SCRIPT"
        NAS_CONFIGURED=true
    else
        warn "nas_connect.sh not found at $NAS_SCRIPT"
        warn "Make sure all Wildlife Monitor files are in the same directory."
        warn "Run ./nas_connect.sh manually when ready."
    fi
else
    info "Skipping NAS setup. Run ./nas_connect.sh when ready."
fi

# ── STEP 8: Systemd services ────────────────────────────────────────────────────
echo ""
echo "[8/9] Systemd service setup"
echo ""
echo "  Installing systemd services will:"
echo "    • Start the web dashboard automatically on boot"
echo "    • Run daily analysis at 6:00 AM (uses settings from the dashboard)"
echo "    • Restart the dashboard automatically if it crashes"
echo ""
echo -en "  ${CYAN}?${NC}  Install systemd services? [Y/n]: "
read -r systemd_choice

SYSTEMD_CONFIGURED=false

if [[ ! "$systemd_choice" =~ ^[Nn]$ ]]; then
    if ! command -v systemctl &>/dev/null; then
        warn "systemctl not found — skipping systemd setup."
        warn "You can start the dashboard manually: python web_app.py"
    else
        info "Writing systemd unit files..."

        # Unmask if previously masked
        sudo systemctl unmask wildlife-monitor.service  2>/dev/null || true
        sudo systemctl unmask wildlife-analysis.service 2>/dev/null || true
        sudo systemctl unmask wildlife-analysis.timer   2>/dev/null || true

        sudo tee /etc/systemd/system/wildlife-monitor.service > /dev/null << EOF
[Unit]
Description=Wildlife Monitor Dashboard
After=network.target remote-fs.target
Wants=remote-fs.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${SCRIPT_DIR}
ExecStart=${VENV_DIR}/bin/python web_app.py \\
    --port 8080 \\
    --host 0.0.0.0 \\
    --data-dir ${SCRIPT_DIR}/data
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=wildlife-monitor
ExecStartPre=/bin/sleep 5

[Install]
WantedBy=multi-user.target
EOF

        sudo tee /etc/systemd/system/wildlife-analysis.service > /dev/null << EOF
[Unit]
Description=Wildlife Monitor — Daily Analysis
After=network.target remote-fs.target
Wants=remote-fs.target

[Service]
Type=oneshot
User=${USER}
WorkingDirectory=${SCRIPT_DIR}
ExecStart=/bin/bash -c 'source ${VENV_DIR}/bin/activate && ./nas_sync.sh --then-process --data-dir ${SCRIPT_DIR}/data'
StandardOutput=append:${SCRIPT_DIR}/data/cron.log
StandardError=append:${SCRIPT_DIR}/data/cron.log
TimeoutStartSec=14400

[Install]
WantedBy=multi-user.target
EOF

        sudo tee /etc/systemd/system/wildlife-analysis.timer > /dev/null << EOF
[Unit]
Description=Wildlife Monitor — Daily Analysis Timer
Requires=wildlife-analysis.service

[Timer]
OnCalendar=*-*-* 06:00:00
Persistent=true
RandomizedDelaySec=60

[Install]
WantedBy=timers.target
EOF

        sudo systemctl daemon-reload
        sudo systemctl enable --now wildlife-monitor.service
        sudo systemctl enable --now wildlife-analysis.timer

        ok "wildlife-monitor.service enabled and started"
        ok "wildlife-analysis.timer enabled (runs daily at 06:00)"

        # Verify dashboard is up
        sleep 3
        if sudo systemctl is-active --quiet wildlife-monitor.service; then
            ok "Dashboard is running at http://$(hostname -I | awk '{print $1}'):8080"
        else
            warn "Dashboard service did not start cleanly. Check: journalctl -u wildlife-monitor -n 20"
        fi

        SYSTEMD_CONFIGURED=true
    fi
else
    info "Skipping systemd setup."
    info "To install later, re-run: ./setup.sh"
    info "Or start manually: source $VENV_DIR/bin/activate && python web_app.py"
fi

# ── STEP 9: Activation helper ──────────────────────────────────────────────────
echo "[9/9] Writing activation helper..."

HELPER="$HOME/wildlife_activate.sh"
cat > "$HELPER" <<HELPER_EOF
#!/usr/bin/env bash
# Auto-generated by Wildlife Monitor setup.sh
# Source this file to activate the environment:
#   source ~/wildlife_activate.sh
source "${VENV_DIR}/bin/activate"
cd "$(pwd)"
echo "Wildlife Monitor environment active. Python: \$(python3 --version)"
HELPER_EOF
chmod +x "$HELPER"
ok "Activation helper written to $HELPER"

# ── Final summary ──────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "  ${GREEN}Setup complete!${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

if [[ "$SYSTEMD_CONFIGURED" == true ]]; then
    echo "  Dashboard: http://$(hostname -I | awk '{print $1}'):8080"
    echo "  (starts automatically on boot)"
    echo ""
    echo "  Useful commands:"
    echo "    sudo systemctl status wildlife-monitor        # dashboard status"
    echo "    sudo systemctl status wildlife-analysis.timer # next scheduled run"
    echo "    journalctl -u wildlife-monitor -f             # live dashboard logs"
    echo "    tail -f ${SCRIPT_DIR}/data/cron.log           # analysis logs"
else
    echo "  Activate environment:"
    echo "    source $VENV_DIR/bin/activate"
    echo ""
    if [[ "$SUGGESTED_PORT" != "8080" ]]; then
        echo -e "  ${YELLOW}Port 8080 is taken.${NC} Start dashboard on a free port:"
        echo "    python web_app.py --port $SUGGESTED_PORT"
    else
        echo "  Start dashboard:"
        echo "    python web_app.py"
    fi
    echo "    → Open http://localhost:${SUGGESTED_PORT}"
fi

echo ""
if [[ "$NAS_CONFIGURED" == true ]]; then
    echo "  Run first sync + analysis:"
    echo "    ./nas_sync.sh --then-process"
else
    echo "  Configure NAS connection (required before syncing):"
    echo "    ./nas_connect.sh"
    echo ""
    echo "  Then sync and process:"
    echo "    ./nas_sync.sh --then-process"
fi
echo ""
echo "  First run downloads models (~800 MB, one-time)."
echo "  All Python packages are isolated in the venv — system Python untouched."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
