#!/usr/bin/env bash
# ==============================================================================
#  Wildlife Monitor — NAS Connection Setup
#
#  Configures a connection to a Synology NAS and stores credentials securely.
#  Run this once, then use nas_sync.sh to pull videos before each processing run.
#
#  Supports: NFS (recommended), SMB/CIFS
#
#  Usage:
#    ./nas_connect.sh              # Interactive setup
#    ./nas_connect.sh --test       # Test existing connection only
#    ./nas_connect.sh --show       # Show current saved config (passwords hidden)
#    ./nas_connect.sh --unmount    # Unmount the NAS share
# ==============================================================================
set -euo pipefail

# ── Paths ──────────────────────────────────────────────────────────────────────
CONFIG_DIR="$HOME/.config/wildlife_monitor"
CONFIG_FILE="$CONFIG_DIR/nas.conf"
CREDS_FILE="$CONFIG_DIR/nas_smb_creds"   # SMB only, chmod 600
DEFAULT_MOUNT="/mnt/wildlife_nas"

# ── Colours ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "  ${CYAN}→${NC}  $*"; }
ok()      { echo -e "  ${GREEN}✓${NC}  $*"; }
warn()    { echo -e "  ${YELLOW}!${NC}  $*"; }
err()     { echo -e "  ${RED}✗${NC}  $*"; }
heading() { echo -e "\n${BOLD}$*${NC}"; }
ask()     { echo -en "  ${CYAN}?${NC}  $* "; }

# ── Argument parsing ───────────────────────────────────────────────────────────
MODE="setup"
case "${1:-}" in
    --test)    MODE="test" ;;
    --show)    MODE="show" ;;
    --unmount) MODE="unmount" ;;
    --help|-h)
        echo "Usage: $0 [--test] [--show] [--unmount]"
        echo "  (no args)   Interactive setup wizard"
        echo "  --test      Test connection with saved config"
        echo "  --show      Display saved config (passwords hidden)"
        echo "  --unmount   Unmount the NAS share"
        exit 0 ;;
esac

# ── Load existing config ───────────────────────────────────────────────────────
load_config() {
    [[ -f "$CONFIG_FILE" ]] && source "$CONFIG_FILE" || true
}

# ── Show config ────────────────────────────────────────────────────────────────
if [[ "$MODE" == "show" ]]; then
    load_config
    if [[ ! -f "$CONFIG_FILE" ]]; then
        err "No config found at $CONFIG_FILE. Run $0 to set up."; exit 1
    fi
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Wildlife Monitor — NAS Config"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Protocol    : ${NAS_PROTOCOL:-?}"
    echo "  NAS Host    : ${NAS_HOST:-?}"
    echo "  Share Path  : ${NAS_SHARE:-?}"
    echo "  Mount Point : ${NAS_MOUNT:-?}"
    echo "  Video Subdir: ${NAS_VIDEO_SUBDIR:-(root of share)}"
    echo "  Archive Dir : ${NAS_ARCHIVE_SUBDIR:-wildlife_archive}"
    if [[ "${NAS_PROTOCOL:-}" == "smb" ]]; then
        echo "  SMB User    : ${NAS_SMB_USER:-?}"
        echo "  SMB Password: ••••••••"
        echo "  Creds file  : $CREDS_FILE"
    fi
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    exit 0
fi

# ── Unmount ────────────────────────────────────────────────────────────────────
if [[ "$MODE" == "unmount" ]]; then
    load_config
    MOUNT="${NAS_MOUNT:-$DEFAULT_MOUNT}"
    if mountpoint -q "$MOUNT" 2>/dev/null; then
        sudo umount "$MOUNT"
        ok "Unmounted $MOUNT"
    else
        warn "$MOUNT is not currently mounted."
    fi
    exit 0
fi

# ── Test existing connection ───────────────────────────────────────────────────
test_connection() {
    local mount="$1"
    local video_subdir="${2:-}"
    local test_path="$mount"
    [[ -n "$video_subdir" ]] && test_path="$mount/$video_subdir"

    if ! mountpoint -q "$mount" 2>/dev/null; then
        err "Share is not mounted at $mount"; return 1
    fi
    if [[ ! -d "$test_path" ]]; then
        err "Video directory not found: $test_path"; return 1
    fi

    # No depth limit — structure is camera/year/month/day/video.mp4 (4 levels deep)
    local file_count
    file_count=$(find "$test_path" \( -name "*.mp4" -o -name "*.avi" -o -name "*.mov" -o -name "*.mkv" -o -name "*.mts" -o -name "*.ts" -o -name "*.wmv" \) 2>/dev/null | wc -l || echo "0")

    # Also show discovered cameras (top-level subdirectories)
    local cameras
    cameras=$(find "$test_path" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | sort | xargs -I{} basename {} 2>/dev/null | tr '\n' '  ' || echo "(none)")

    ok "Mount accessible: $mount"
    ok "Camera folders detected: $cameras"
    ok "Video files visible (all cameras): $file_count"
    return 0
}

if [[ "$MODE" == "test" ]]; then
    load_config
    if [[ ! -f "$CONFIG_FILE" ]]; then
        err "No config found. Run $0 first."; exit 1
    fi
    echo ""
    info "Testing NAS connection..."

    # Mount if not already mounted
    CREDS_FILE_TEST="$HOME/.config/wildlife_monitor/nas_smb_creds"
    if ! mountpoint -q "${NAS_MOUNT}" 2>/dev/null; then
        warn "${NAS_MOUNT} is not mounted. Attempting to mount..."
        sudo mkdir -p "${NAS_MOUNT}"
        if [[ "${NAS_PROTOCOL}" == "nfs" ]]; then
            sudo mount -t nfs -o "rw,soft,timeo=30,retrans=3,rsize=131072,wsize=131072,noatime" \
                "${NAS_HOST}:${NAS_SHARE}" "${NAS_MOUNT}"
        else
            sudo mount -t cifs \
                -o "credentials=${CREDS_FILE_TEST},uid=$(id -u),gid=$(id -g),iocharset=utf8,file_mode=0644,dir_mode=0755,noatime" \
                "//${NAS_HOST}/${NAS_SHARE}" "${NAS_MOUNT}"
        fi
        ok "Mounted ${NAS_MOUNT}"
    fi

    test_connection "${NAS_MOUNT}" "${NAS_VIDEO_SUBDIR:-}"
    exit $?
fi

# ── Interactive setup wizard ───────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Wildlife Monitor — NAS Connection Setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Load existing values as defaults for re-runs
load_config

# ── 1. NAS host ────────────────────────────────────────────────────────────────
heading "Step 1/7 — NAS address"
echo "  Enter the hostname or IP address of your Synology NAS."
echo "  Examples: 192.168.1.100   nas.local   diskstation.local"
echo ""
ask "NAS hostname or IP [${NAS_HOST:-}]:"
read -r input
NAS_HOST="${input:-${NAS_HOST:-}}"
if [[ -z "$NAS_HOST" ]]; then
    err "NAS host is required."; exit 1
fi

# Quick reachability check
echo ""
info "Checking if $NAS_HOST is reachable..."
if ping -c 2 -W 3 "$NAS_HOST" &>/dev/null 2>&1; then
    ok "$NAS_HOST is reachable"
else
    warn "$NAS_HOST did not respond to ping."
    warn "This could be normal if ICMP is blocked. Continuing anyway."
fi

# ── 2. Protocol ────────────────────────────────────────────────────────────────
heading "Step 2/7 — Protocol"
echo "  Which protocol should we use to connect?"
echo ""
echo "    1) NFS  (recommended for Linux — no credentials needed, better performance)"
echo "    2) SMB  (Windows-style shares — requires username + password)"
echo ""
CURRENT_PROTO="${NAS_PROTOCOL:-nfs}"
ask "Choose [1=NFS / 2=SMB] (current: $CURRENT_PROTO):"
read -r proto_choice
case "${proto_choice:-}" in
    2|smb|SMB) NAS_PROTOCOL="smb" ;;
    1|nfs|NFS|"") NAS_PROTOCOL="nfs" ;;
    *) warn "Unrecognised choice, defaulting to NFS."; NAS_PROTOCOL="nfs" ;;
esac
ok "Using protocol: $NAS_PROTOCOL"

# ── 3. Share path ──────────────────────────────────────────────────────────────
heading "Step 3/7 — Share path"
if [[ "$NAS_PROTOCOL" == "nfs" ]]; then
    echo "  NFS share path on the Synology (e.g. /volume1/cameras)"
    echo "  Find this in: Synology DSM → Control Panel → File Services → NFS"
    echo "  → Make sure NFS is enabled and the share has an NFS rule for this machine."
    echo ""
    ask "NFS export path [${NAS_SHARE:-/volume1/cameras}]:"
    read -r input
    NAS_SHARE="${input:-${NAS_SHARE:-/volume1/cameras}}"
else
    echo "  SMB share name on the Synology (just the share name, e.g. cameras)"
    echo "  This is the folder name visible in Windows Explorer / Finder."
    echo ""
    ask "SMB share name [${NAS_SHARE:-cameras}]:"
    read -r input
    NAS_SHARE="${input:-${NAS_SHARE:-cameras}}"
fi

# ── 4. SMB credentials ─────────────────────────────────────────────────────────
if [[ "$NAS_PROTOCOL" == "smb" ]]; then
    heading "Step 4/7 — SMB credentials"
    echo "  Credentials are stored in $CREDS_FILE (chmod 600, readable only by you)."
    echo "  Use a Synology user with read access to the share."
    echo "  Tip: create a dedicated read-only user on the NAS for this."
    echo ""
    ask "Synology username [${NAS_SMB_USER:-}]:"
    read -r input
    NAS_SMB_USER="${input:-${NAS_SMB_USER:-}}"
    if [[ -z "$NAS_SMB_USER" ]]; then
        err "Username required for SMB."; exit 1
    fi

    ask "Password (hidden):"
    read -rs NAS_SMB_PASS
    echo ""
    if [[ -z "$NAS_SMB_PASS" ]]; then
        err "Password required for SMB."; exit 1
    fi

    ask "SMB domain/workgroup (leave blank for WORKGROUP/default):"
    read -r NAS_SMB_DOMAIN
    NAS_SMB_DOMAIN="${NAS_SMB_DOMAIN:-WORKGROUP}"
else
    heading "Step 4/7 — NFS (no credentials needed)"
    ok "NFS uses IP-based access control — no username/password required."
    warn "Make sure your Synology NFS rule allows access from this machine's IP."
    warn "DSM → Control Panel → Shared Folder → [share] → NFS Permissions → Add rule"
    NAS_SMB_USER=""
    NAS_SMB_PASS=""
    NAS_SMB_DOMAIN=""
fi

# ── 5. Mount point ─────────────────────────────────────────────────────────────
heading "Step 5/7 — Local mount point"
echo "  Where should the NAS share be mounted locally?"
echo "  A new directory will be created here if it doesn't exist."
echo ""
ask "Mount point [${NAS_MOUNT:-$DEFAULT_MOUNT}]:"
read -r input
NAS_MOUNT="${input:-${NAS_MOUNT:-$DEFAULT_MOUNT}}"

# Check if the mount point is already in use by something else
if mountpoint -q "$NAS_MOUNT" 2>/dev/null; then
    EXISTING=$(mount | grep " $NAS_MOUNT " | head -1 || true)
    warn "$NAS_MOUNT is already mounted:"
    warn "  $EXISTING"
    ask "Unmount it and replace with the NAS share? [y/N]:"
    read -r yn
    if [[ "$yn" =~ ^[Yy]$ ]]; then
        sudo umount "$NAS_MOUNT"
        ok "Unmounted existing share"
    else
        err "Aborted. Choose a different mount point."; exit 1
    fi
fi

# ── 6. Video source subdirectory ──────────────────────────────────────────────
heading "Step 6/7 — Source video subdirectory"
echo "  If your camera recordings are in a subfolder within the share, enter it here."
echo "  Leave blank if camera folders are directly at the root of the share."
echo "  Example: recordings   or   cameras/raw"
echo ""
ask "Source subdirectory [${NAS_VIDEO_SUBDIR:-none}]:"
read -r input
NAS_VIDEO_SUBDIR="${input:-${NAS_VIDEO_SUBDIR:-}}"

# Derived full source path
if [[ -n "$NAS_VIDEO_SUBDIR" ]]; then
    VIDEO_PATH="$NAS_MOUNT/$NAS_VIDEO_SUBDIR"
else
    VIDEO_PATH="$NAS_MOUNT"
fi

# ── 7. Archive folder ──────────────────────────────────────────────────────────
heading "Step 7/7 — Archive folder for processed footage"
echo "  After processing, videos with animal/person detections are moved"
echo "  to an archive folder on the NAS instead of keeping them locally."
echo "  This keeps your Ubuntu machine's disk free while preserving all footage."
echo ""
echo "  The archive is organised automatically as:"
echo "    <archive folder>/<camera>/<year>/<month>/<day>/<file>"
echo ""
echo "  Use a folder name within the same share (e.g. wildlife_archive)."
echo "  It will be created automatically on first use."
echo ""
ask "Archive folder name [${NAS_ARCHIVE_SUBDIR:-wildlife_archive}]:"
read -r input
NAS_ARCHIVE_SUBDIR="${input:-${NAS_ARCHIVE_SUBDIR:-wildlife_archive}}"

# Warn if archive path would overlap with source path
ARCHIVE_FULL="$NAS_MOUNT/$NAS_ARCHIVE_SUBDIR"
if [[ "$ARCHIVE_FULL" == "$VIDEO_PATH" || "$VIDEO_PATH" == "$ARCHIVE_FULL"* ]]; then
    err "Archive folder cannot be the same as or inside the source folder."
    err "  Source : $VIDEO_PATH"
    err "  Archive: $ARCHIVE_FULL"
    err "Please re-run and choose a different archive name."
    exit 1
fi
ok "Archive path: $ARCHIVE_FULL"

# ── Save config ────────────────────────────────────────────────────────────────
echo ""
info "Saving configuration..."
mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"

cat > "$CONFIG_FILE" <<EOF
# Wildlife Monitor — NAS Configuration
# Generated by nas_connect.sh on $(date)
# Edit this file or re-run nas_connect.sh to change settings.

NAS_HOST="$NAS_HOST"
NAS_PROTOCOL="$NAS_PROTOCOL"
NAS_SHARE="$NAS_SHARE"
NAS_MOUNT="$NAS_MOUNT"
NAS_VIDEO_SUBDIR="$NAS_VIDEO_SUBDIR"
NAS_ARCHIVE_SUBDIR="$NAS_ARCHIVE_SUBDIR"
NAS_SMB_USER="$NAS_SMB_USER"
NAS_SMB_DOMAIN="$NAS_SMB_DOMAIN"
EOF
chmod 600 "$CONFIG_FILE"
ok "Config saved to $CONFIG_FILE (chmod 600)"

# Save SMB credentials separately (extra protection)
if [[ "$NAS_PROTOCOL" == "smb" ]]; then
    cat > "$CREDS_FILE" <<EOF
username=$NAS_SMB_USER
password=$NAS_SMB_PASS
domain=$NAS_SMB_DOMAIN
EOF
    chmod 600 "$CREDS_FILE"
    ok "SMB credentials saved to $CREDS_FILE (chmod 600)"
fi

# ── Install required packages ──────────────────────────────────────────────────
echo ""
info "Checking required system packages..."
if [[ "$NAS_PROTOCOL" == "smb" ]]; then
    if ! dpkg -s cifs-utils &>/dev/null 2>&1; then
        info "Installing cifs-utils (required for SMB mounts)..."
        sudo apt-get install -y -qq cifs-utils
        ok "cifs-utils installed"
    else
        ok "cifs-utils already installed"
    fi
else
    if ! dpkg -s nfs-common &>/dev/null 2>&1; then
        info "Installing nfs-common (required for NFS mounts)..."
        sudo apt-get install -y -qq nfs-common
        ok "nfs-common installed"
    else
        ok "nfs-common already installed"
    fi
fi

# ── Mount function ─────────────────────────────────────────────────────────────
mount_nas() {
    sudo mkdir -p "$NAS_MOUNT"

    if [[ "$NAS_PROTOCOL" == "nfs" ]]; then
        info "Mounting NFS share: ${NAS_HOST}:${NAS_SHARE} → ${NAS_MOUNT}"
        sudo mount -t nfs \
            -o "rw,soft,timeo=30,retrans=3,rsize=131072,wsize=131072,noatime" \
            "${NAS_HOST}:${NAS_SHARE}" "$NAS_MOUNT"
    else
        info "Mounting SMB share: //${NAS_HOST}/${NAS_SHARE} → ${NAS_MOUNT}"
        sudo mount -t cifs \
            -o "credentials=${CREDS_FILE},uid=$(id -u),gid=$(id -g),iocharset=utf8,file_mode=0644,dir_mode=0755,noatime,cache=strict" \
            "//${NAS_HOST}/${NAS_SHARE}" "$NAS_MOUNT"
    fi
}

# ── Test mount ─────────────────────────────────────────────────────────────────
echo ""
info "Testing connection..."
mount_nas

if test_connection "$NAS_MOUNT" "$NAS_VIDEO_SUBDIR"; then
    ok "Connection successful!"
else
    err "Mount succeeded but video directory test failed."
    err "Check the subdirectory path and NAS permissions."
    exit 1
fi

# ── fstab entry ────────────────────────────────────────────────────────────────
echo ""
heading "Auto-mount on boot (fstab)"
echo "  Add an fstab entry so the NAS mounts automatically at boot?"
echo "  This is recommended for unattended/cron operation."
echo ""
warn "This will add ONE line to /etc/fstab. Your existing fstab is preserved."
warn "A backup will be saved to /etc/fstab.wildlife.bak before any change."
echo ""
ask "Add fstab entry? [y/N]:"
read -r yn
if [[ "$yn" =~ ^[Yy]$ ]]; then
    # Build fstab line
    if [[ "$NAS_PROTOCOL" == "nfs" ]]; then
        FSTAB_LINE="${NAS_HOST}:${NAS_SHARE}  ${NAS_MOUNT}  nfs  rw,soft,timeo=30,retrans=3,rsize=131072,wsize=131072,noatime,noauto,x-systemd.automount  0  0"
    else
        FSTAB_LINE="//${NAS_HOST}/${NAS_SHARE}  ${NAS_MOUNT}  cifs  credentials=${CREDS_FILE},uid=$(id -u),gid=$(id -g),iocharset=utf8,file_mode=0644,dir_mode=0755,noatime,noauto,x-systemd.automount  0  0"
    fi

    # Check if an entry for this mount point already exists
    if grep -q "[ \t]${NAS_MOUNT}[ \t]" /etc/fstab 2>/dev/null; then
        warn "An existing fstab entry for $NAS_MOUNT was found:"
        grep "[ \t]${NAS_MOUNT}[ \t]" /etc/fstab
        ask "Replace it? [y/N]:"
        read -r replace
        if [[ "$replace" =~ ^[Yy]$ ]]; then
            sudo cp /etc/fstab /etc/fstab.wildlife.bak
            sudo sed -i "\|[ \t]${NAS_MOUNT}[ \t]|d" /etc/fstab
            echo "$FSTAB_LINE" | sudo tee -a /etc/fstab > /dev/null
            ok "fstab entry replaced (backup at /etc/fstab.wildlife.bak)"
        else
            info "fstab unchanged."
        fi
    else
        sudo cp /etc/fstab /etc/fstab.wildlife.bak
        echo "" | sudo tee -a /etc/fstab > /dev/null
        echo "# Wildlife Monitor NAS — added $(date)" | sudo tee -a /etc/fstab > /dev/null
        echo "$FSTAB_LINE" | sudo tee -a /etc/fstab > /dev/null
        ok "fstab entry added (backup at /etc/fstab.wildlife.bak)"
    fi

    # Reload systemd so the automount unit picks up the new entry
    if command -v systemctl &>/dev/null; then
        sudo systemctl daemon-reload 2>/dev/null || true
        ok "systemd reloaded"
    fi
else
    info "fstab not modified. The NAS will need to be mounted manually after reboots:"
    if [[ "$NAS_PROTOCOL" == "nfs" ]]; then
        echo "    sudo mount -t nfs -o rw,soft ${NAS_HOST}:${NAS_SHARE} ${NAS_MOUNT}"
    else
        echo "    sudo mount -t cifs -o credentials=${CREDS_FILE} //${NAS_HOST}/${NAS_SHARE} ${NAS_MOUNT}"
    fi
fi

# ── Final summary ──────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "  ${GREEN}NAS setup complete!${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  NAS share mounted at : $NAS_MOUNT"
echo "  Source video path    : $VIDEO_PATH"
echo "  Archive path         : $NAS_MOUNT/$NAS_ARCHIVE_SUBDIR"
echo "    Structure          : <archive>/<camera>/<year>/<month>/<day>/<file>"
echo ""
echo "  Workflow:"
echo "    1. nas_sync.sh copies raw videos to Ubuntu for processing"
echo "    2. Processor detects animals/people, deletes empty footage"
echo "    3. Kept videos move to the archive folder on the NAS"
echo "    4. Ubuntu staging copy is deleted — NAS stores everything"
echo ""
echo "  Run everything at once:"
echo "    ./nas_sync.sh --then-process --country US"
echo ""
echo "  To re-run this wizard:  ./nas_connect.sh"
echo "  To test the connection: ./nas_connect.sh --test"
echo "  To show saved config:   ./nas_connect.sh --show"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
