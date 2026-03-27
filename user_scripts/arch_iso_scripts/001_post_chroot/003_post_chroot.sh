#!/usr/bin/env bash
#
# Arch Linux Configuration Script (Chroot Phase)
# Optimized for Bash 5+ | Arch Linux | UWSM/Hyprland Context
#

# --- 1. Safety & Environment ---
set -euo pipefail
IFS=$'\n\t'

# --- 2. Visuals & Helpers ---
readonly BOLD=$'\e[1m'
readonly RESET=$'\e[0m'
readonly GREEN=$'\e[32m'
readonly BLUE=$'\e[34m'
readonly RED=$'\e[31m'
readonly YELLOW=$'\e[33m'

log_info() { printf "${BLUE}[INFO]${RESET} %s\n" "$1"; }
log_success() { printf "${GREEN}[SUCCESS]${RESET} %s\n" "$1"; }
log_error() { printf "${RED}[ERROR]${RESET} %s\n" "$1"; }
log_step() { printf "\n${BOLD}${YELLOW}>>> STEP: %s${RESET}\n" "$1"; }

trap 'printf "${RESET}\n"' EXIT

# --- 3. Pre-flight Check (Deterministic Validation) ---
log_step "Environment Validation"

if command -v findmnt &>/dev/null; then
    FSTYPE=$(findmnt -no FSTYPE / 2>/dev/null || true)
    if [[ "$FSTYPE" =~ ^(overlay|airootfs)$ ]]; then
        log_error "Execution halted: Detected Live ISO ($FSTYPE)."
        printf "Please run: ${BOLD}arch-chroot /mnt${RESET} first.\n"
        exit 1
    fi
else
    if [[ "$(stat -c %d:%i /)" == "$(stat -c %d:%i /proc/1/root/.)" ]]; then
        log_error "Execution halted: Not running inside a chroot."
        exit 1
    fi
fi
log_success "Chroot environment confirmed."

# --- 4. Resilient Timezone Resolution ---
get_dynamic_timezone() {
    local tz=""
    local fallback_tz="Asia/Kolkata" 
    
    if command -v curl &>/dev/null; then
        tz=$(curl -sSfL --retry 3 --retry-delay 1 --connect-timeout 3 https://ipapi.co/timezone 2>/dev/null || true)
        if [[ -z "$tz" ]]; then
            tz=$(curl -sSfL --retry 2 --connect-timeout 3 http://ip-api.com/line?fields=timezone 2>/dev/null || true)
        fi
    fi

    if [[ -n "$tz" && -f "/usr/share/zoneinfo/$tz" ]]; then
        echo "$tz"
    else
        echo "$fallback_tz"
    fi
}

# --- 5. Data Ingestion (Gatekeeper) ---
log_step "Configuration Ingestion"

TARGET_TZ="${TARGET_TZ:-$(get_dynamic_timezone)}"

if [[ -z "${TARGET_HOSTNAME:-}" ]]; then
    read -r -p "Enter hostname [Default: ${BOLD}workstation${RESET}]: " INPUT_HOST
    FINAL_HOST="${INPUT_HOST:-workstation}"
else
    FINAL_HOST="$TARGET_HOSTNAME"
fi

if [[ -z "${TARGET_USER:-}" ]]; then
    read -r -p "Enter username [Default: ${BOLD}dusk${RESET}]: " INPUT_USER
    FINAL_USER="${INPUT_USER:-dusk}"
else
    FINAL_USER="$TARGET_USER"
fi

if [[ -z "${ROOT_PASS:-}" ]]; then
    read -r -s -p "Enter ROOT password: " ROOT_PASS
    echo
fi

if [[ -z "${USER_PASS:-}" ]]; then
    read -r -s -p "Enter password for user '$FINAL_USER': " USER_PASS
    echo
fi

if [[ -z "$ROOT_PASS" || -z "$USER_PASS" ]]; then
    log_error "Passwords cannot be empty. Aborting deployment."
    exit 1
fi

readonly TARGET_TZ FINAL_HOST FINAL_USER ROOT_PASS USER_PASS

log_success "Parameters secured. Proceeding with headless deployment..."

# --- 6. Main Execution (Headless & Idempotent) ---

# === System Time ===
log_step "Configuring Timezone: $TARGET_TZ"
ln -sf "/usr/share/zoneinfo/$TARGET_TZ" /etc/localtime
hwclock --systohc
log_success "Timezone linked and hardware clock synced."

# === System Language ===
log_step "Configuring Locales"
sed -i 's/^#\?\s*\(en_US.UTF-8\s\+UTF-8\)/\1/' /etc/locale.gen
locale-gen
printf "LANG=en_US.UTF-8\n" > /etc/locale.conf
log_success "System language generated and configured."

# === Hostname ===
log_step "Setting Hostname"
printf "%s\n" "$FINAL_HOST" > /etc/hostname
log_success "Hostname set to: $FINAL_HOST"

# === Root Password ===
log_step "Setting Root Password"
echo "root:${ROOT_PASS}" | chpasswd
log_success "Root credentials secured."

# === User Account ===
log_step "Provisioning User: $FINAL_USER"
pacman -S --needed --noconfirm zsh

if id "$FINAL_USER" &>/dev/null; then
    log_info "User '$FINAL_USER' exists. Verifying state..."
    
    # Idempotent Shell Verification
    CURRENT_SHELL=$(getent passwd "$FINAL_USER" | cut -d: -f7)
    if [[ "$CURRENT_SHELL" != "/usr/bin/zsh" ]]; then
        log_info "Enforcing ZSH as default shell..."
        usermod -s /usr/bin/zsh "$FINAL_USER"
    fi
else
    useradd -m -G wheel,input,audio,video,storage,optical,network,lp,power,games,rfkill -s /usr/bin/zsh "$FINAL_USER"
fi

echo "${FINAL_USER}:${USER_PASS}" | chpasswd
log_success "User account provisioned and secured."

# === Wheel Group Rights ===
log_step "Configuring Sudoers"
printf '%%wheel ALL=(ALL:ALL) ALL\n' | EDITOR='tee' visudo -f /etc/sudoers.d/10_wheel >/dev/null
chmod 0440 /etc/sudoers.d/10_wheel
log_success "Wheel group privileges granted."

printf "\n${GREEN}${BOLD}Post-Chroot configuration complete. Proceeding to next orchestrator step...${RESET}\n"
