#!/usr/bin/env bash
# ==============================================================================
# 030_build_iso.sh - THE FACTORY ISO GENERATOR
# Architecture: Bypasses airootfs RAM exhaustion via dynamic mkarchiso patching.
# ==============================================================================
set -euo pipefail

# --- 1. CONFIGURATION ---
readonly ZRAM_DIR="/mnt/zram1/dusky_iso"
readonly PROFILE_DIR="${ZRAM_DIR}/profile"
readonly WORK_DIR="${ZRAM_DIR}/work"
readonly OUT_DIR="${ZRAM_DIR}/out"
# Change this if your merged Official + AUR repo is located elsewhere
readonly OFFLINE_REPO_DIR="/srv/offline-repo"
readonly MKARCHISO_CUSTOM="${ZRAM_DIR}/mkarchiso_dusky"
readonly PATCH_FILE="${ZRAM_DIR}/repo_inject.patch"

# --- 2. PRE-FLIGHT CHECKS ---
if (( EUID != 0 )); then
    echo "[INFO] Root required — re-launching under sudo..."
    exec sudo "$0" "$@"
fi

if [[ ! -d "$OFFLINE_REPO_DIR" ]]; then
    echo "[ERR] Offline repository not found at $OFFLINE_REPO_DIR!" >&2
    exit 1
fi

# Verify the injection point exists in the installed mkarchiso before we touch
# anything. This catches archiso upgrades that rename or refactor the function,
# preventing a silent build that produces an ISO missing the offline repo.
if ! grep -q '^_build_iso_image() {' /usr/bin/mkarchiso; then
    echo "[ERR] Could not locate '_build_iso_image() {' in /usr/bin/mkarchiso." >&2
    echo "[ERR] The archiso package may have been updated and renamed this function." >&2
    echo "[ERR] Inspect /usr/bin/mkarchiso, find the correct ISO-assembly function," >&2
    echo "[ERR] and update the sed pattern in this script accordingly." >&2
    exit 1
fi

echo -e "\n\e[1;34m==>\e[0m \e[1mINITIATING DUSKY ARCH ISO FACTORY BUILD\e[0m\n"

# --- 3. DYNAMIC MKARCHISO PATCHING (The payload) ---
echo "  -> Cloning official mkarchiso..."
cp /usr/bin/mkarchiso "$MKARCHISO_CUSTOM"
chmod +x "$MKARCHISO_CUSTOM"

echo "  -> Generating injection patch..."
# We create a patch file. The variables \$isofs_dir and \$install_dir
# are escaped so they are evaluated by mkarchiso at runtime, not right now.
# ${OFFLINE_REPO_DIR} is intentionally NOT escaped — it expands now and is
# hardcoded as an absolute path into the patch, which is correct behaviour.
cat << EOF > "$PATCH_FILE"
    _msg_info ">>> INJECTING 2-3GB+ OFFLINE REPOSITORY (Bypassing RAM/airootfs) <<<"
    mkdir -p "\${isofs_dir}/\${install_dir}/repo"
    cp -a "${OFFLINE_REPO_DIR}/." "\${isofs_dir}/\${install_dir}/repo/"
    _msg_info ">>> INJECTION COMPLETE <<<"
EOF

echo "  -> Splicing hook into mkarchiso pipeline..."
# sed 'r' inserts the patch file's contents immediately after the matched line,
# leaving the function declaration itself intact.
sed -i '/^_build_iso_image() {/r '"$PATCH_FILE"'' "$MKARCHISO_CUSTOM"

# sed exits 0 whether or not the pattern matched. Verify the patch actually
# landed before proceeding — a missing injection would produce a silent,
# wrong ISO with no error from the build itself.
if ! grep -q 'INJECTING 2-3GB+ OFFLINE REPOSITORY' "$MKARCHISO_CUSTOM"; then
    echo "[ERR] Patch was NOT injected — the sed pattern failed to match." >&2
    echo "[ERR] Inspect $MKARCHISO_CUSTOM to diagnose." >&2
    exit 1
fi
echo "  -> Patch verified successfully."

# --- 4. ISO GENERATION ---
echo "  -> Cleaning previous build artifacts..."
rm -rf "$WORK_DIR" "$OUT_DIR"

echo -e "\n\e[1;32m==>\e[0m \e[1mSTARTING BUILD PROCESS\e[0m"
# -m iso: explicitly target ISO mode only, rather than deferring to whatever
# buildmodes=() the profile declares. Prevents accidental netboot/bootstrap
# builds if the profile lists multiple modes.
"$MKARCHISO_CUSTOM" -v -m iso -w "$WORK_DIR" -o "$OUT_DIR" "$PROFILE_DIR"

echo -e "\n\e[1;32m[SUCCESS]\e[0m \e[1mISO generation complete!\e[0m"
echo "Your bootable ISO is located in: $OUT_DIR"
