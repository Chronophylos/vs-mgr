#!/bin/bash
#=============================================================================
# Vintage Story Server Management Script (vs_manage.sh)
# Version: 1.1.0
#=============================================================================
# Description:
#   A comprehensive tool for managing Vintage Story game servers including
#   updating server versions, creating backups, and checking for new releases.
#
# Author: chrono (with assistance)
# License: MIT
#
# Usage:
#   ./vs_manage.sh <command> [options]
#
# Available commands:
#   update         - Update the server to a specific version
#   info           - Display information about the current installation
#   check-version  - Check for available updates
#
# Global options:
#   --dry-run           - Simulate operations without making changes
#   --generate-config   - Generate a sample configuration file
#
# Configuration:
#   The script can be configured using a config file or environment variables.
#   Run ./vs_manage.sh --generate-config to create a sample config file.
#   Supported locations: ./vs_manage.conf, ~/.config/vs_manage/config, /etc/vs_manage.conf
#
# For more details, run:
#   ./vs_manage.sh --help
#=============================================================================

set -e          # Exit immediately if a command exits with a non-zero status
set -o pipefail # Pipe failures propagate correctly

# Define colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# --- Configuration ---
SERVICE_NAME="vintagestoryserver"
SERVER_DIR="/srv/gameserver/vintagestory"
DATA_DIR="/srv/gameserver/data/vs"
SERVER_USER="gameserver"
TMP_DIR="/tmp/vs_update"
BACKUP_DIR="/srv/gameserver/backups"
MAX_BACKUPS=10 # Number of backups to keep
LOG_DIR="/var/log/vs_manage"
LOG_FILE="${LOG_DIR}/vs_manage.log"
VERSION_CHECK_URL="https://cdn.vintagestory.at/gamefiles"
GAME_VERSIONS_API="https://mods.vintagestory.at/api/gameversions"
API_VERSION_CHECK_ENABLED=true # Whether to use API for version checking if available
# --- End Configuration ---

# Config file locations (in order of precedence)
CONFIG_FILES=(
    "./vs_manage.conf"                 # Local directory
    "${HOME}/.config/vs_manage/config" # User config directory
    "/etc/vs_manage.conf"              # System-wide config
)
# --- End Configuration ---

# Global state variables
SERVER_STOPPED=false
ARCHIVE_NAME=""
DRY_RUN=false         # Whether to simulate operations without making changes
IS_ROOT=false         # Whether the script is being run as root
RSYNC_AVAILABLE=false # Whether rsync is available for use

# Check if script is being run as root/sudo
check_root() {
    if [ "$(id -u)" -eq 0 ]; then
        IS_ROOT=true
        return 0
    else
        IS_ROOT=false
        return 1
    fi
}

# Load configuration from config file
load_config() {
    local config_loaded=false

    # Process environment variables first (highest precedence)
    if [ -n "${VS_SERVICE_NAME}" ]; then SERVICE_NAME="${VS_SERVICE_NAME}"; fi
    if [ -n "${VS_SERVER_DIR}" ]; then SERVER_DIR="${VS_SERVER_DIR}"; fi
    if [ -n "${VS_DATA_DIR}" ]; then DATA_DIR="${VS_DATA_DIR}"; fi
    if [ -n "${VS_SERVER_USER}" ]; then SERVER_USER="${VS_SERVER_USER}"; fi
    if [ -n "${VS_TMP_DIR}" ]; then TMP_DIR="${VS_TMP_DIR}"; fi
    if [ -n "${VS_BACKUP_DIR}" ]; then BACKUP_DIR="${VS_BACKUP_DIR}"; fi
    if [ -n "${VS_MAX_BACKUPS}" ]; then MAX_BACKUPS="${VS_MAX_BACKUPS}"; fi
    if [ -n "${VS_LOG_DIR}" ]; then LOG_DIR="${VS_LOG_DIR}"; fi
    if [ -n "${VS_VERSION_CHECK_URL}" ]; then VERSION_CHECK_URL="${VS_VERSION_CHECK_URL}"; fi

    # Then check configuration files
    for config_file in "${CONFIG_FILES[@]}"; do
        if [ -f "${config_file}" ]; then
            echo -e "${CYAN}Loading configuration from ${config_file}...${NC}"
            # shellcheck source=/dev/null
            if source "${config_file}"; then
                config_loaded=true
                log_message "INFO" "Loaded configuration from ${config_file}"
                break
            else
                echo -e "${YELLOW}Warning: Failed to load configuration from ${config_file}${NC}" >&2
                log_message "WARNING" "Failed to load configuration from ${config_file}"
            fi
        fi
    done

    if [ "${config_loaded}" = "false" ]; then
        echo -e "${YELLOW}No configuration file found, using default values.${NC}" >&2
        log_message "INFO" "Using default configuration values"
    fi

    # Update LOG_FILE based on potentially updated LOG_DIR
    LOG_FILE="${LOG_DIR}/vs_manage.log"
}

# Execute a command with sudo if needed
run_with_sudo() {
    if [ "${IS_ROOT}" = "true" ]; then
        "$@"
    else
        sudo "$@"
    fi
}

# Log a message with timestamp to the log file
log_message() {
    local level="$1"
    local message="$2"
    local timestamp
    timestamp=$(date "+%Y-%m-%d %H:%M:%S")

    # Create log directory if it doesn't exist
    if [ ! -d "${LOG_DIR}" ]; then
        run_mkdir "${LOG_DIR}"
    fi

    # Log to file if not in dry run mode
    if [ "${DRY_RUN}" = "false" ]; then
        local log_dir
        log_dir=$(dirname "${LOG_FILE}")
        # Check dir writability for creating file, or file writability if it exists
        if { [ -w "${log_dir}" ] && [ ! -e "${LOG_FILE}" ]; } || [ -w "${LOG_FILE}" ]; then
            # We can write directly
            echo -e "${timestamp} [${level}] ${message}" >>"${LOG_FILE}"
        else
            # Use sudo tee if not writable directly
            echo -e "${timestamp} [${level}] ${message}" | run_with_sudo tee -a "${LOG_FILE}" >/dev/null
        fi
    fi

    # Always echo to console for INFO, WARNING, ERROR
    case "${level}" in
    INFO)
        echo -e "${CYAN}${message}${NC}"
        ;;
    WARNING)
        echo -e "${YELLOW}WARNING: ${message}${NC}" >&2
        ;;
    ERROR)
        echo -e "${RED}ERROR: ${message}${NC}" >&2
        ;;
    DEBUG)
        if [ "${DEBUG_MODE:-false}" = "true" ]; then
            echo -e "${BLUE}DEBUG: ${message}${NC}" >&2
        fi
        ;;
    esac
}

# Check if the systemd service exists
check_service_exists() {
    local service_name="$1"
    if systemctl list-unit-files "${service_name}.service" | grep -q "${service_name}.service"; then
        return 0
    else
        return 1
    fi
}

# Tries to determine the latest version by checking the official API.
get_latest_version() {
    local channel="$1" # stable or unstable

    echo -e "${CYAN}Checking for latest version via official API...${NC}" >&2
    local api_response
    if ! api_response=$(wget -q -O - "${GAME_VERSIONS_API}" 2>/dev/null); then
        echo -e "${RED}Error: Could not connect to game versions API at ${GAME_VERSIONS_API}${NC}" >&2
        echo -e "${YELLOW}Check your internet connection and try again.${NC}" >&2
        return 1
    fi

    # Check if the response is valid JSON and contains gameversions
    if [[ "${api_response}" != *"gameversions"* ]]; then
        echo -e "${RED}Error: Invalid response from API${NC}" >&2
        return 1
    fi

    echo -e "${GREEN}✓ Successfully retrieved version data from API${NC}" >&2

    # Extract the version names from the JSON response
    # Use jq if available for better parsing, fall back to grep/sed
    local versions
    if command -v jq &>/dev/null; then
        log_message "DEBUG" "Using jq for API version parsing."
        if [ "${channel}" = "stable" ]; then
            # Filter out release candidates and pre-releases for stable channel
            versions=$(echo "${api_response}" | jq -r '.gameversions[].name' | grep -v -E "(-rc|-pre)")
        else
            # Include all versions for unstable channel
            versions=$(echo "${api_response}" | jq -r '.gameversions[].name')
        fi
    else
        log_message "DEBUG" "Using grep/sed for API version parsing (jq not found)."
        versions=$(echo "${api_response}" | grep -o '"name":"[^"]*"' | sed 's/"name":"//g' | sed 's/"//g')
    fi

    # Find the latest stable version (not containing "rc", "pre", etc.)
    local latest_stable=""
    local latest_stable_without_v=""

    while read -r version; do
        # Skip release candidates, pre-releases, etc. for stable channel
        # Still need this check when using grep/sed method
        if [ "${channel}" = "stable" ] && [[ "${version}" == *"-rc"* || "${version}" == *"-pre"* ]]; then
            continue
        fi

        # Remove 'v' prefix for comparison
        local version_without_v="${version#v}"

        if [ -z "${latest_stable}" ]; then
            latest_stable="${version}"
            latest_stable_without_v="${version_without_v}"
        else
            # Compare versions
            comparison=$(compare_versions "v${latest_stable_without_v}" "v${version_without_v}")
            if [ "${comparison}" = "older" ]; then
                latest_stable="${version}"
                latest_stable_without_v="${version_without_v}"
            fi
        fi
    done <<<"${versions}"

    if [ -z "${latest_stable_without_v}" ]; then
        echo -e "${RED}Error: Could not determine latest ${channel} version from API response${NC}" >&2
        return 1
    fi

    echo -e "${GREEN}Latest ${channel} version from API: ${latest_stable} (${latest_stable_without_v})${NC}" >&2

    # Verify that this version has a downloadable server package
    local download_url="${VERSION_CHECK_URL}/${channel}/vs_server_linux-x64_${latest_stable_without_v}.tar.gz"
    if ! wget --spider -q "${download_url}" 2>/dev/null; then
        echo -e "${RED}Error: Found version ${latest_stable} via API, but no download available at ${download_url}${NC}" >&2
        return 1
    fi

    echo -e "${GREEN}✓ Verified download URL: ${download_url}${NC}" >&2
    echo "${latest_stable_without_v}"
    return 0
}

# Compares two semantic version strings (e.g., v1.2.3 or 1.2.3).
# Outputs "newer", "older", or "same".
compare_versions() {
    local ver1="$1"
    local ver2="$2"

    ver1=${ver1#v} # Remove 'v' prefix if present
    ver2=${ver2#v}

    IFS='.' read -ra VER1 <<<"$ver1"
    IFS='.' read -ra VER2 <<<"$ver2"

    for i in {0..2}; do
        # Handle cases where one version string has fewer parts (e.g., 1.19 vs 1.19.1) by defaulting to 0
        local v1_part=${VER1[$i]:-0}
        local v2_part=${VER2[$i]:-0}

        if [ "${v1_part}" -gt "${v2_part}" ]; then
            echo "newer"
            return
        elif [ "${v1_part}" -lt "${v2_part}" ]; then
            echo "older"
            return
        fi
    done

    echo "same"
}

show_check_version_usage() {
    echo -e "${CYAN}Usage: $0 check-version [options]${NC}"
    echo -e "${CYAN}Check if a new version of Vintage Story is available.${NC}"
    echo -e ""
    echo -e "${CYAN}Options:${NC}"
    echo -e "${CYAN}  --channel <stable|unstable>  Check for versions in the specified channel (default: stable)${NC}"
    exit 1
}

cmd_check_version() {
    if [ "$#" -gt 0 ] && [ "$1" = "--help" ]; then
        show_check_version_usage
    fi

    local channel="stable"

    while [ "$#" -gt 0 ]; do
        case "$1" in
        --channel)
            if [ "$#" -lt 2 ]; then
                echo -e "${RED}Error: --channel requires an argument${NC}" >&2
                show_check_version_usage
            fi
            if [ "$2" != "stable" ] && [ "$2" != "unstable" ]; then
                echo -e "${RED}Error: channel must be 'stable' or 'unstable'${NC}" >&2
                show_check_version_usage
            fi
            channel="$2"
            shift 2
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}" >&2
            show_check_version_usage
            ;;
        esac
    done

    echo -e "${GREEN}=== Vintage Story Version Check ===${NC}"
    echo -e "${CYAN}Checking for latest available version in the ${channel} channel...${NC}"

    local current_version
    if ! current_version=$(get_server_version); then
        # Warning already printed by get_server_version
        echo -e "${YELLOW}Version comparison will not be available.${NC}" >&2
        current_version="unknown"
    else
        echo -e "${CYAN}Current server version: ${NC}${current_version}"
    fi

    local latest_version
    if ! latest_version=$(get_latest_version "${channel}"); then
        echo -e "${RED}Error: Could not determine latest available version.${NC}" >&2
        echo -e "${YELLOW}Check your internet connection and try again.${NC}" >&2
        exit 1
    fi

    echo -e "${CYAN}Latest available version: ${NC}v${latest_version}"

    if [ "${current_version}" != "unknown" ]; then
        local comparison
        comparison=$(compare_versions "${current_version}" "v${latest_version}")

        case "${comparison}" in
        newer)
            echo -e "${GREEN}✓ Your server is running a newer version than the latest ${channel} release.${NC}"
            ;;
        same)
            echo -e "${GREEN}✓ Your server is up to date with the latest ${channel} release.${NC}"
            ;;
        older)
            echo -e "${YELLOW}! A newer version is available. Consider updating your server.${NC}"
            echo -e "${CYAN}Update command: ${NC}$0 update ${latest_version}"
            ;;
        esac
    fi

    # As a final sanity check, confirm the derived download URL is actually accessible
    local update_url="${VERSION_CHECK_URL}/${channel}/vs_server_linux-x64_${latest_version}.tar.gz"
    if wget --spider -q "${update_url}"; then
        echo -e "${GREEN}✓ Update file URL verified: ${update_url}${NC}"
    else
        echo -e "${YELLOW}⚠ Warning: Could not confirm availability of update file URL: ${update_url}${NC}" >&2
        # Don't exit, just warn. The version likely exists, but URL might be temporarily down or filename pattern changed.
    fi
}

check_dependencies() {
    local missing_deps=()
    local critical_deps=("wget" "tar" "zstd" "systemctl") # Critical requirements
    local recommended_deps=("rsync")                      # Strongly recommended
    local opt_deps=("dotnet" "jq")                        # Optional with fallbacks

    echo -e "${CYAN}Checking for required dependencies...${NC}"

    for dep in "${critical_deps[@]}"; do
        if ! command -v "${dep}" &>/dev/null; then
            missing_deps+=("${dep}")
        fi
    done

    if [ ${#missing_deps[@]} -gt 0 ]; then
        echo -en "${RED}Error: Missing required dependencies: " >&2
        echo -n "${missing_deps[@]}" >&2 # Print array elements separated by spaces
        echo -e "${NC}" >&2
        echo -e "${YELLOW}Please install these dependencies before running this script.${NC}" >&2
        exit 1
    fi

    # Check for recommended dependencies
    for dep in "${recommended_deps[@]}"; do
        if command -v "${dep}" &>/dev/null; then
            case "${dep}" in
            rsync)
                RSYNC_AVAILABLE=true
                echo -e "${GREEN}✓ rsync is available (recommended for safer updates)${NC}"
                ;;
            esac
        else
            case "${dep}" in
            rsync)
                RSYNC_AVAILABLE=false
                echo -e "${RED}⚠ IMPORTANT: rsync is not installed!${NC}" >&2
                echo -e "${RED}  Server updates will use a fallback method that is LESS SAFE and could potentially cause data loss.${NC}" >&2
                echo -e "${RED}  It is STRONGLY RECOMMENDED to install rsync before proceeding with updates.${NC}" >&2
                echo -e "${YELLOW}  On most systems, you can install it with: apt install rsync (Debian/Ubuntu) or yum install rsync (RHEL/CentOS)${NC}" >&2

                # Prompt for confirmation if not in dry-run mode
                if [ "${DRY_RUN}" = "false" ]; then
                    echo -e "${YELLOW}Do you want to continue without rsync? (y/N)${NC}" >&2
                    read -r response
                    if [[ ! "$response" =~ ^[Yy]$ ]]; then
                        echo -e "${CYAN}Exiting. Please install rsync and try again.${NC}" >&2
                        exit 1
                    fi
                    echo -e "${YELLOW}Proceeding without rsync (not recommended)...${NC}" >&2
                fi
                ;;
            esac
        fi
    done

    for dep in "${opt_deps[@]}"; do
        if ! command -v "${dep}" &>/dev/null; then
            echo -e "${YELLOW}Note: Optional dependency '${dep}' not found.${NC}" >&2
            case "${dep}" in
            dotnet)
                echo -e "${YELLOW}  Some version checking features will be limited.${NC}" >&2
                echo -e "${YELLOW}  Consider installing dotnet for direct version verification.${NC}" >&2
                ;;
            jq)
                echo -e "${YELLOW}  JSON parsing for version checks will use basic methods.${NC}" >&2
                echo -e "${YELLOW}  Consider installing jq for better version API handling if available.${NC}" >&2
                ;;
            esac
        fi
    done

    echo -e "${GREEN}All required dependencies are available.${NC}"
}

# --- Wrapper functions for common operations ---

# Wrapper for systemctl operations
run_systemctl() {
    local action="$1"
    local service="$2"
    local msg="systemctl ${action} ${service}.service"

    log_message "DEBUG" "Executing ${msg}"

    if [ "${DRY_RUN}" = "true" ]; then
        echo -e "${BLUE}[DRY RUN] Would run: ${msg}${NC}" >&2
        return 0 # Simulate success
    else
        if run_with_sudo systemctl "${action}" "${service}.service"; then
            log_message "INFO" "${msg} successful"
            return 0
        else
            log_message "ERROR" "${msg} failed"
            return 1
        fi
    fi
}

# Wrapper for directory creation
run_mkdir() {
    local dir="$1"
    local msg="mkdir -p ${dir}"

    log_message "DEBUG" "Creating directory: ${dir}"

    if [ "${DRY_RUN}" = "true" ]; then
        echo -e "${BLUE}[DRY RUN] Would create directory: ${dir}${NC}" >&2
        return 0 # Simulate success
    else
        if mkdir -p "${dir}" 2>/dev/null || run_with_sudo mkdir -p "${dir}"; then
            log_message "INFO" "Created directory: ${dir}"
            return 0
        else
            log_message "ERROR" "Failed to create directory: ${dir}"
            return 1
        fi
    fi
}

# Wrapper for chmod operations
run_chown() {
    local owner="$1"
    local target="$2"
    local recursive=${3:-false}
    local r_flag=""
    local msg=""

    if [ "${recursive}" = "true" ]; then
        r_flag="-R"
        msg="chown -R ${owner} ${target}"
    else
        msg="chown ${owner} ${target}"
    fi

    log_message "DEBUG" "Executing ${msg}"

    if [ "${DRY_RUN}" = "true" ]; then
        echo -e "${BLUE}[DRY RUN] Would run: ${msg}${NC}" >&2
        return 0 # Simulate success
    else
        if run_with_sudo chown ${r_flag} "${owner}" "${target}"; then
            log_message "INFO" "${msg} successful"
            return 0
        else
            log_message "WARNING" "${msg} failed"
            return 1 # Return failure but don't exit, as this is often non-critical
        fi
    fi
}

# --- End wrapper functions ---

show_usage() {
    echo -e "${CYAN}Vintage Story Server Management Script${NC}"
    echo -e "${CYAN}Usage: $0 <command> [options]${NC}"
    echo -e ""
    echo -e "${CYAN}Available commands:${NC}"
    echo -e "  ${GREEN}update${NC}        Update the Vintage Story server to a specified version"
    echo -e "  ${GREEN}info${NC}          Display information about the currently installed server"
    echo -e "  ${GREEN}check-version${NC} Check if a new version of Vintage Story is available"
    # Future commands can be added here
    echo -e ""
    echo -e "${CYAN}Global options:${NC}"
    echo -e "  ${GREEN}--dry-run${NC}           Simulate operations without making changes"
    echo -e "  ${GREEN}--generate-config${NC}   Generate a sample configuration file"
    echo -e ""
    echo -e "${CYAN}Configuration:${NC}"
    echo -e "  The script can be configured using a config file or environment variables."
    echo -e "  Run ${GREEN}$0 --generate-config${NC} to create a sample configuration file."
    echo -e ""
    echo -e "${CYAN}For command-specific help, use:${NC}"
    echo -e "  $0 <command> --help"
}

show_update_usage() {
    echo -e "${CYAN}Usage: $0 update <version_number> [options]${NC}"
    echo -e "${CYAN}Example: $0 update 1.20.7${NC}"
    echo -e ""
    echo -e "${CYAN}Options:${NC}"
    echo -e "${CYAN}  --skip-backup             Skip creating a backup${NC}"
    echo -e "${CYAN}  --ignore-backup-failure   Continue even if backup fails${NC}"
    echo -e "${CYAN}  --max-backups <num>       Number of backups to keep (default: ${MAX_BACKUPS})${NC}"
    echo -e "${CYAN}  --dry-run                 Simulate the update without making changes${NC}"
    exit 1
}

# Ensures temporary files are cleaned up and attempts to restart the server if stopped during a failed operation.
cleanup() {
    local exit_code="${1:-0}" # Capture exit code that triggered the trap

    # Clean up temporary update directory
    if [ -n "${TMP_DIR}" ] && [ -d "${TMP_DIR}" ] && [ "${TMP_DIR}" != "/" ]; then
        echo -e "${BLUE}Cleaning up temporary directory: ${TMP_DIR}${NC}" >&2
        if [ "${DRY_RUN}" = "false" ]; then
            rm -rf "${TMP_DIR}"
        else
            echo -e "${BLUE}[DRY RUN] Would remove temporary directory: ${TMP_DIR}${NC}" >&2
        fi
    fi

    # Clean up downloaded archive
    if [ -n "${ARCHIVE_NAME}" ] && [ -f "/tmp/${ARCHIVE_NAME}" ]; then
        echo -e "${BLUE}Cleaning up downloaded archive: /tmp/${ARCHIVE_NAME}${NC}" >&2
        if [ "${DRY_RUN}" = "false" ]; then
            rm -f "/tmp/${ARCHIVE_NAME}"
        else
            echo -e "${BLUE}[DRY RUN] Would remove archive: /tmp/${ARCHIVE_NAME}${NC}" >&2
        fi
    fi

    # Attempt restart only if the server was stopped by *this script* (SERVER_STOPPED=true)
    # and is *not currently running* (implies script exited before successful start or failed during start).
    # This prevents restarting if the script exited before stopping the server, or if it started successfully.
    if [ "${SERVER_STOPPED}" = "true" ] && ! systemctl is-active --quiet "${SERVICE_NAME}.service"; then
        echo -e "${YELLOW}Attempting to restart server (${SERVICE_NAME}) after script interruption/error...${NC}" >&2
        if check_service_exists "${SERVICE_NAME}"; then
            if run_systemctl start "${SERVICE_NAME}"; then
                echo -e "${GREEN}Server restart command issued successfully.${NC}" >&2
                log_message "INFO" "Server ${SERVICE_NAME} restarted after script interruption."
            else
                echo -e "${RED}Failed to issue server restart command. Check status manually: systemctl status ${SERVICE_NAME}.service${NC}" >&2
                log_message "ERROR" "Failed to restart server ${SERVICE_NAME} after script interruption."
            fi
        else
            echo -e "${YELLOW}Service ${SERVICE_NAME} does not exist. Cannot restart.${NC}" >&2
            log_message "WARNING" "Cannot restart non-existent service ${SERVICE_NAME}."
        fi
    fi

    exit "${exit_code}" # Exit with the original or default exit code
}

create_backup() {
    local ignore_failure="$1"

    BACKUP_TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    BACKUP_FILE="${BACKUP_DIR}/vs_data_backup_${BACKUP_TIMESTAMP}.tar.zst"

    echo -e "${CYAN}Calculating size of data directory (${DATA_DIR})...${NC}"
    DATA_SIZE=$(du -sh "${DATA_DIR}" 2>/dev/null | cut -f1)
    echo -e "${CYAN}Data size: ${NC}${YELLOW}${DATA_SIZE:-N/A}${NC}"
    echo -e "${CYAN}Creating backup: ${NC}${BACKUP_FILE}"

    run_mkdir "${BACKUP_DIR}"

    # Exclude cache/log/existing backup directories from the archive
    if tar --exclude="${DATA_DIR}/Backups" \
        --exclude="${DATA_DIR}/BackupSave" \
        --exclude="${DATA_DIR}/Cache" \
        --exclude="${DATA_DIR}/Logs" \
        -cf - -C "$(dirname "${DATA_DIR}")" "$(basename "${DATA_DIR}")" | zstd -z -9 -o "${BACKUP_FILE}"; then

        BACKUP_SIZE=$(du -sh "${BACKUP_FILE}" 2>/dev/null | cut -f1)
        echo -e "${GREEN}Backup created successfully: ${BACKUP_FILE} (${BACKUP_SIZE:-N/A})${NC}"
        run_chown "${SERVER_USER}:${SERVER_USER}" "${BACKUP_FILE}" # Set ownership

        # Rotate backups: keep only the most recent MAX_BACKUPS
        if [ "${MAX_BACKUPS}" -gt 0 ]; then
            echo -e "${CYAN}Rotating backups (keeping ${MAX_BACKUPS} most recent)...${NC}"

            # Improved backup rotation - more readable approach
            local old_backups
            old_backups=$(find "${BACKUP_DIR}" -maxdepth 1 -name 'vs_data_backup_*.tar.zst' -printf '%T@ %p\n' |
                sort -nr |
                awk -v max="${MAX_BACKUPS}" 'NR > max {print $2}')

            if [ -n "${old_backups}" ]; then
                echo -e "${CYAN}Removing ${YELLOW}$(echo "${old_backups}" | wc -l)${NC} ${CYAN}old backups...${NC}"
                echo "${old_backups}" | xargs rm -f
            else
                echo -e "${CYAN}No backups to rotate (total count <= ${MAX_BACKUPS}).${NC}"
            fi
        fi
    else
        echo -e "${RED}ERROR: Backup creation failed!${NC}" >&2
        rm -f "${BACKUP_FILE}" # Clean up potentially incomplete/empty backup file
        if [ "${ignore_failure}" = "false" ]; then
            echo -e "${YELLOW}To proceed without a backup, run with --skip-backup or --ignore-backup-failure${NC}" >&2
            return 1 # Signal failure
        else
            echo -e "${YELLOW}Continuing despite backup failure (--ignore-backup-failure was specified)${NC}" >&2
            return 0 # Allow continuation, but return no backup file path (caller should check if empty)
        fi
    fi

    echo "${BACKUP_FILE}" # Return the path to the created backup file on success
    return 0
}

# Waits briefly and checks if the server service is active. Returns 0 on success, 1 on failure.
check_server_status() {
    echo -e "${CYAN}Checking server status (${SERVICE_NAME})...${NC}"
    for i in {1..5}; do
        sleep 3
        if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
            echo -e "${GREEN}Server is running.${NC}"
            return 0 # Success
        fi
        if [ "${i}" -eq 5 ]; then
            echo -e "${RED}WARNING: Server did not report active status after 5 checks.${NC}" >&2
            echo -e "${YELLOW}Check status manually: systemctl status ${SERVICE_NAME}.service${NC}" >&2
            return 1 # Failure: Server didn't become active
        fi
        echo -e "${YELLOW}Waiting for server status (attempt $i of 5)...${NC}"
    done
    # This part should ideally not be reached due to the check for i=5 inside the loop
    echo -e "${RED}Error: Loop finished unexpectedly in check_server_status.${NC}" >&2
    return 1
}

# Attempts to get the installed server version using the --version flag. Requires dotnet.
get_server_version() {
    local dll_path="${SERVER_DIR}/VintagestoryServer.dll"
    if [ ! -f "${dll_path}" ]; then
        echo -e "${YELLOW}⚠ Server executable not found: ${dll_path}${NC}" >&2
        return 1
    fi

    if ! command -v dotnet &>/dev/null; then
        echo -e "${YELLOW}⚠ Cannot check version: 'dotnet' command not found.${NC}" >&2
        return 1
    fi

    local version_output
    # Use subshell for cd to avoid changing script's directory; redirect stderr of dotnet
    if ! version_output=$( (cd "${SERVER_DIR}" && dotnet VintagestoryServer.dll --version) 2>/dev/null) || [ -z "${version_output}" ]; then
        echo -e "${YELLOW}⚠ Failed to get server version using --version flag (check permissions or dotnet install).${NC}" >&2
        return 1
    fi

    # Extract version (handles formats like 'v1.2.3' or 'Game Version: v1.2.3' or just '1.2.3')
    local version
    version=$(echo "${version_output}" | grep -o -E 'v?[0-9]+\.[0-9]+\.[0-9]+' | head -n 1)

    if [ -z "${version}" ]; then
        echo -e "${YELLOW}⚠ Could not parse version from output: ${version_output}${NC}" >&2
        return 1
    fi

    # Ensure 'v' prefix for consistency internally
    if [[ ! "${version}" == v* ]]; then
        version="v${version}"
    fi

    echo "${version}" # Output version with 'v' prefix
    return 0
}

# Verifies if the running server version matches the expected version.
# Tries direct --version check first, falls back to log file check. Returns 0 on match, 1 otherwise.
verify_server_version() {
    local expected_version="$1"                     # Should be passed without 'v' prefix (e.g., 1.20.7)
    local expected_version_v="v${expected_version}" # Add 'v' for comparison
    echo -e "${CYAN}Verifying server version (expecting ${expected_version_v})...${NC}"

    local installed_version
    if installed_version=$(get_server_version); then
        echo -e "${CYAN}Detected server version via --version: ${NC}${installed_version}"
        if [ "${installed_version}" = "${expected_version_v}" ]; then
            echo -e "${GREEN}✓ Server is running the expected version ${installed_version}${NC}"
            return 0 # Success: Direct check matches
        else
            echo -e "${YELLOW}⚠ WARNING: Server reports version ${installed_version}, but expected ${expected_version_v}${NC}" >&2
            echo -e "${YELLOW}  The update might not have fully applied or direct check is inaccurate. Will check logs.${NC}" >&2
            # Don't return failure yet, proceed to log check
        fi
    else
        # Warning already printed by get_server_version
        echo -e "${YELLOW}Could not get version via --version flag. Proceeding to log check.${NC}" >&2
    fi

    # Fallback: Check log file (useful if --version fails or gives wrong result initially)
    echo -e "${YELLOW}Falling back to log file check for version verification...${NC}"
    LOG_FILE="${DATA_DIR}/Logs/server-main.log"

    sleep 2 # Wait a moment for log file to potentially update after start

    if [ -f "${LOG_FILE}" ]; then
        local log_version
        # Grep for the line indicating game version, extract only the version number (e.g., v1.2.3)
        log_version=$(grep -m 1 "Game Version: v" "${LOG_FILE}" | grep -o -E 'v[0-9]+\.[0-9]+\.[0-9]+')

        if [ -n "${log_version}" ]; then
            echo -e "${CYAN}Detected server version from log: ${NC}${log_version}"
            if [ "${log_version}" = "${expected_version_v}" ]; then
                echo -e "${GREEN}✓ Server log confirms expected version ${log_version}${NC}"
                return 0 # Success: Log check matches
            else
                echo -e "${YELLOW}⚠ WARNING: Server log shows version ${log_version}, but expected ${expected_version_v}${NC}" >&2
                echo -e "${YELLOW}  The update likely did not apply correctly.${NC}" >&2
                return 1 # Failure: Log mismatch
            fi
        else
            echo -e "${YELLOW}⚠ Could not detect server version from log file (${LOG_FILE}). Verification incomplete.${NC}" >&2
            return 1 # Failure: Cannot verify from log
        fi
    else
        echo -e "${YELLOW}⚠ Log file not found: ${LOG_FILE}. Cannot verify version from log.${NC}" >&2
        return 1 # Failure: Cannot verify from log
    fi
}

show_info_usage() {
    echo -e "${CYAN}Usage: $0 info [options]${NC}"
    echo -e "${CYAN}Display information about the currently installed Vintage Story server.${NC}"
    echo -e ""
    echo -e "${CYAN}Options:${NC}"
    echo -e "${CYAN}  --detailed      Show additional server information (sizes, service status)${NC}"
    exit 1
}

cmd_info() {
    if [ "$#" -gt 0 ] && [ "$1" = "--help" ]; then
        show_info_usage
    fi

    local detailed=false
    while [ "$#" -gt 0 ]; do
        case "$1" in
        --detailed)
            detailed=true
            shift
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}" >&2
            show_info_usage
            ;;
        esac
    done

    echo -e "${GREEN}=== Vintage Story Server Information ===${NC}"

    if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
        echo -e "${CYAN}Server Status:    ${GREEN}Running${NC}"
    else
        echo -e "${CYAN}Server Status:    ${YELLOW}Stopped${NC}"
    fi

    local server_version
    if server_version=$(get_server_version); then
        echo -e "${CYAN}Server Version:   ${GREEN}${server_version}${NC}"
    else
        # If get_server_version failed, it already printed a warning
        echo -e "${CYAN}Server Version:   ${YELLOW}Unknown (could not determine)${NC}"
    fi

    echo -e "${CYAN}Server Directory: ${NC}${SERVER_DIR}"
    echo -e "${CYAN}Data Directory:   ${NC}${DATA_DIR}"
    echo -e "${CYAN}Backup Directory: ${NC}${BACKUP_DIR}"

    if [ "${detailed}" = "true" ]; then
        echo -e "\n${CYAN}--- Detailed Information ---${NC}"

        if [ -d "${SERVER_DIR}" ]; then
            local server_size
            server_size=$(du -sh "${SERVER_DIR}" 2>/dev/null | cut -f1)
            echo -e "${CYAN}Server files size:  ${NC}${server_size:-N/A}"
        fi
        if [ -d "${DATA_DIR}" ]; then
            local data_size
            data_size=$(du -sh "${DATA_DIR}" 2>/dev/null | cut -f1)
            echo -e "${CYAN}Data directory size:${NC}${data_size:-N/A}"
        fi
        if [ -d "${BACKUP_DIR}" ]; then
            # Optimized backup count: check if at least one exists quickly, then count if needed
            local backup_count=0
            if find "${BACKUP_DIR}" -maxdepth 1 -name "vs_data_backup_*.tar.zst" -print -quit 2>/dev/null | grep -q .; then
                backup_count=$(find "${BACKUP_DIR}" -maxdepth 1 -name "vs_data_backup_*.tar.zst" | wc -l)
            fi
            local backup_size
            backup_size=$(du -sh "${BACKUP_DIR}" 2>/dev/null | cut -f1)
            echo -e "${CYAN}Backup count:       ${NC}${backup_count}"
            echo -e "${CYAN}Backup dir size:    ${NC}${backup_size:-N/A}"
        fi

        echo -e "\n${CYAN}--- Service Status (${SERVICE_NAME}) ---${NC}"
        # Show first few lines of status output, handle case where service doesn't exist gracefully
        systemctl status "${SERVICE_NAME}.service" --no-pager | head -n 3 || echo -e "${YELLOW}Could not retrieve service status (service might not exist or permissions issue).${NC}" >&2
    fi
}

cmd_update() {
    if [ "$#" -eq 0 ] || [ "$1" = "--help" ]; then
        show_update_usage
    fi

    local NEW_VERSION="$1"
    local SKIP_BACKUP=false
    local IGNORE_BACKUP_FAILURE=false
    local BACKUP_FILE="" # Will hold the path to the backup if created successfully

    # Validate version format (simple check: X.Y.Z)
    if [[ ! "$NEW_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo -e "${RED}Error: Invalid version format: '${NEW_VERSION}'. Expected format X.Y.Z${NC}" >&2
        exit 1
    fi

    shift # Consume version argument
    while [ "$#" -gt 0 ]; do
        case "$1" in
        --skip-backup)
            SKIP_BACKUP=true
            shift
            ;;
        --ignore-backup-failure)
            IGNORE_BACKUP_FAILURE=true
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --max-backups)
            if [ "$#" -lt 2 ] || [[ ! "$2" =~ ^[0-9]+$ ]]; then
                echo -e "${RED}Error: --max-backups requires a non-negative numeric argument${NC}" >&2
                exit 1
            fi
            MAX_BACKUPS="$2"
            shift 2
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}" >&2
            show_update_usage
            ;; # Exit via the usage function
        esac
    done

    DOWNLOAD_URL="${VERSION_CHECK_URL}/stable/vs_server_linux-x64_${NEW_VERSION}.tar.gz"
    ARCHIVE_NAME="vs_server_linux-x64_${NEW_VERSION}.tar.gz" # Used for downloading and cleanup

    echo -e "${GREEN}=== Vintage Story Server Update ===${NC}"
    log_message "INFO" "Starting update to version ${NEW_VERSION}"

    if [ "${DRY_RUN}" = "true" ]; then
        echo -e "${BLUE}[DRY RUN MODE] Simulating update without making changes${NC}"
        log_message "INFO" "Running in dry-run mode (simulation only)"
    fi

    echo -e "${CYAN}Target version:   ${NC}${NEW_VERSION}"
    echo -e "${CYAN}Server directory: ${NC}${SERVER_DIR}"
    echo -e "${CYAN}Data directory:   ${NC}${DATA_DIR}"

    # Check root privileges
    check_root
    if [ "${IS_ROOT}" = "false" ]; then
        log_message "INFO" "Running without root privileges, sudo will be used when required"
    fi

    # Check service existence
    if ! check_service_exists "${SERVICE_NAME}"; then
        echo -e "${RED}Error: Service ${SERVICE_NAME} does not exist. Please check the service name.${NC}" >&2
        log_message "ERROR" "Service ${SERVICE_NAME} does not exist."
        exit 1
    else
        log_message "INFO" "Service ${SERVICE_NAME} exists."
    fi

    # Dependency check specifically for backup compression
    if ! command -v zstd &>/dev/null && [ "${SKIP_BACKUP}" = "false" ]; then
        echo -e "${RED}Error: 'zstd' is required for backups but not found.${NC}" >&2
        echo -e "${YELLOW}Install zstd or use --skip-backup.${NC}" >&2
        log_message "ERROR" "Missing required dependency: zstd"
        exit 1
    fi

    # Check for rsync one more time before proceeding with update
    if [ "${RSYNC_AVAILABLE}" = "false" ] && [ "${DRY_RUN}" = "false" ]; then
        echo -e "${RED}⚠ WARNING: Proceeding with update without rsync (potentially unsafe)${NC}" >&2
        log_message "WARNING" "Updating without rsync (using less safe fallback method)"
    fi

    echo -e "${CYAN}Verifying download URL: ${DOWNLOAD_URL}${NC}"
    if ! wget --spider -q "${DOWNLOAD_URL}"; then
        echo -e "${RED}Error: Could not access download URL.${NC}" >&2
        echo -e "${RED}Check the version number ('${NEW_VERSION}') and network connection.${NC}" >&2
        log_message "ERROR" "Failed to verify download URL: ${DOWNLOAD_URL}"
        exit 1
    fi
    echo -e "${GREEN}Download URL verified.${NC}"
    log_message "INFO" "Download URL verified: ${DOWNLOAD_URL}"

    echo -e "${CYAN}Stopping server (${SERVICE_NAME})...${NC}"
    if ! run_systemctl stop "${SERVICE_NAME}"; then
        echo -e "${RED}Error: Failed to stop the server service.${NC}" >&2
        echo -e "${YELLOW}Check service status: systemctl status ${SERVICE_NAME}.service${NC}" >&2
        log_message "ERROR" "Failed to stop server service ${SERVICE_NAME}"
        exit 1 # Don't proceed if server couldn't be stopped
    fi
    SERVER_STOPPED=true # Mark that *we* stopped the server (for cleanup logic)
    echo -e "${GREEN}Server stopped.${NC}"

    if [ "${SKIP_BACKUP}" = "false" ]; then
        # create_backup returns the backup path on success, empty on ignored failure, or non-zero exit on critical failure
        if [ "${DRY_RUN}" = "false" ]; then
            if ! backup_result=$(create_backup "${IGNORE_BACKUP_FAILURE}"); then
                # This path is taken only if backup fails AND ignore_failure is false
                echo -e "${RED}Update aborted due to backup failure.${NC}" >&2
                log_message "ERROR" "Update aborted: backup creation failed"
                cleanup 1 # Trigger cleanup (will attempt restart) and exit with error
            fi
            BACKUP_FILE="${backup_result}" # Store the path (might be empty if ignored failure)
            if [ -n "${BACKUP_FILE}" ]; then
                echo -e "${GREEN}Backup step completed successfully.${NC}"
                log_message "INFO" "Backup created successfully at ${BACKUP_FILE}"
            elif [ "${IGNORE_BACKUP_FAILURE}" = "true" ]; then # Backup failed but was ignored
                echo -e "${YELLOW}Backup failed, but continuing as --ignore-backup-failure was specified.${NC}" >&2
                log_message "WARNING" "Backup creation failed but continuing (--ignore-backup-failure)"
            fi
        else
            echo -e "${BLUE}[DRY RUN] Would create backup of data directory${NC}" >&2
            BACKUP_FILE="example_backup_path.tar.zst"
        fi
    else
        echo -e "${YELLOW}Skipping backup as requested.${NC}"
        log_message "INFO" "Backup creation skipped as requested (--skip-backup)"
    fi

    echo -e "${CYAN}Downloading ${ARCHIVE_NAME}...${NC}"
    if ! (cd /tmp && wget -q --show-progress "${DOWNLOAD_URL}" -O "${ARCHIVE_NAME}"); then
        echo -e "${RED}Error: Failed to download server files from ${DOWNLOAD_URL}${NC}" >&2
        cleanup 1
    fi
    echo -e "${GREEN}Download complete (/tmp/${ARCHIVE_NAME}).${NC}"

    echo -e "${CYAN}Extracting files to ${TMP_DIR}...${NC}"
    mkdir -p "${TMP_DIR}"
    rm -rf "${TMP_DIR:?}/"*
    if ! tar -xzf "/tmp/${ARCHIVE_NAME}" -C "${TMP_DIR}"; then
        echo -e "${RED}Error: Failed to extract server files from /tmp/${ARCHIVE_NAME}${NC}" >&2
        cleanup 1
    fi
    echo -e "${GREEN}Extraction complete.${NC}"

    # Sanity check before modifying the actual server directory
    if [ ! -d "${SERVER_DIR}" ] || [ -z "${SERVER_DIR}" ] || [ "${SERVER_DIR}" = "/" ]; then
        echo -e "${RED}CRITICAL ERROR: Invalid SERVER_DIR defined: '${SERVER_DIR}'. Aborting update to prevent data loss.${NC}" >&2
        cleanup 1
    fi

    echo -e "${CYAN}Updating server files in ${SERVER_DIR}...${NC}"
    if [ "${RSYNC_AVAILABLE}" = "true" ]; then
        echo -e "${GREEN}Using rsync for safe, precise updates...${NC}"
        if ! rsync -a --delete \
            --exclude='serverconfig.json' \
            --exclude='Mods/' \
            --exclude='modconfig/' \
            "${TMP_DIR}/" "${SERVER_DIR}/"; then
            echo -e "${RED}Error: rsync failed to update server files in ${SERVER_DIR}${NC}" >&2
            cleanup 1
        fi
    else
        echo -e "${RED}WARNING: Using fallback update method (rsync not available)${NC}" >&2
        echo -e "${RED}This method is less precise but has been improved for safety${NC}" >&2
        echo -e "${YELLOW}It is still recommended to install rsync before proceeding.${NC}" >&2

        if [ "${DRY_RUN}" = "false" ]; then
            echo -e "${YELLOW}Do you want to continue with the fallback method? (y/N)${NC}" >&2
            read -r response
            if [[ ! "$response" =~ ^[Yy]$ ]]; then
                echo -e "${CYAN}Update aborted. Please install rsync and try again.${NC}" >&2
                cleanup 1
            fi
        fi

        echo -e "${YELLOW}Proceeding with improved fallback update method...${NC}" >&2

        # Create a timestamp for the temporary backup directory
        local backup_timestamp
        backup_timestamp=$(date +%Y%m%d_%H%M%S)
        local old_files_dir="${SERVER_DIR}/.old_update_files_${backup_timestamp}"

        echo -e "${BLUE}Moving current server files to temporary location: ${old_files_dir}${NC}"
        if [ "${DRY_RUN}" = "false" ]; then
            # Create backup directory
            mkdir -p "${old_files_dir}"

            # First, create a list of important files to preserve in their original locations
            local preserve_paths=("${SERVER_DIR}/serverconfig.json" "${SERVER_DIR}/Mods" "${SERVER_DIR}/modconfig")

            # Move all files/directories except the preserved ones to the backup directory
            local find_cmd="find \"${SERVER_DIR}\" -mindepth 1 -maxdepth 1 ! -name '.old_update_files_*'"

            # Add exclusions for each preserved path
            for path in "${preserve_paths[@]}"; do
                # Extract just the base name from the full path
                local basename
                basename=$(basename "${path}")
                find_cmd+=" ! -name '${basename}'"
            done

            # Complete the find command with the move action
            find_cmd+=" -exec mv {} \"${old_files_dir}/\" \\; 2>/dev/null"

            # Execute the constructed find command
            if ! eval "${find_cmd}"; then
                echo -e "${RED}Error: Failed to move server files to temporary location.${NC}" >&2
                # Try to clean up the temporary directory
                rm -rf "${old_files_dir}" 2>/dev/null
                cleanup 1
            fi
        else
            echo -e "${BLUE}[DRY RUN] Would move current server files to: ${old_files_dir}${NC}" >&2
        fi

        echo -e "${BLUE}Copying new server files...${NC}"
        if [ "${DRY_RUN}" = "false" ]; then
            if ! cp -r "${TMP_DIR}/"* "${SERVER_DIR}/"; then
                echo -e "${RED}Error: Failed to copy new server files.${NC}" >&2
                echo -e "${YELLOW}Attempting to restore from temporary backup...${NC}" >&2

                # Attempt to restore from the temporary backup
                if cp -r "${old_files_dir}/"* "${SERVER_DIR}/" 2>/dev/null; then
                    echo -e "${GREEN}Restored server files from temporary backup.${NC}" >&2
                else
                    echo -e "${RED}CRITICAL: Failed to restore server files! Server may be in an inconsistent state.${NC}" >&2
                    echo -e "${RED}Manual intervention required. Backup files are at: ${old_files_dir}${NC}" >&2
                fi

                cleanup 1
            fi

            # If copy was successful, and we preserved important directories/files by not moving them,
            # we can now safely remove the temporary backup
            echo -e "${BLUE}Update successful. Cleaning up temporary backup...${NC}"
            rm -rf "${old_files_dir}"
        else
            echo -e "${BLUE}[DRY RUN] Would copy new server files from ${TMP_DIR} to ${SERVER_DIR}${NC}" >&2
            echo -e "${BLUE}[DRY RUN] Would remove temporary backup directory after successful update${NC}" >&2
        fi
    fi

    echo -e "${CYAN}Setting ownership for ${SERVER_DIR} to ${SERVER_USER}:${SERVER_USER}...${NC}"
    run_chown "${SERVER_USER}:${SERVER_USER}" "${SERVER_DIR}" true
    echo -e "${GREEN}Server files updated.${NC}"

    echo -e "${CYAN}Starting server (${SERVICE_NAME})...${NC}"
    if ! run_systemctl start "${SERVICE_NAME}"; then
        echo -e "${RED}Error: Failed to start the server service after update.${NC}" >&2
        echo -e "${YELLOW}Check service status: systemctl status ${SERVICE_NAME}.service${NC}" >&2
        # Don't mark SERVER_STOPPED as false here, as the start failed.
        # cleanup trap will run next; it will see SERVER_STOPPED=true and service inactive -> attempts restart
        cleanup 1 # Exit with error code 1
    fi
    SERVER_STOPPED=false # Mark server as successfully started (or at least, start command issued)
    echo -e "${GREEN}Server start command issued.${NC}"

    # Verify status and version after start. Don't exit on failure here, just warn loudly.
    if ! check_server_status; then
        echo -e "${YELLOW}Warning: Server status check reported potential issues after start.${NC}" >&2
    fi
    if ! verify_server_version "${NEW_VERSION}"; then
        echo -e "${YELLOW}Warning: Server version verification reported potential issues after start.${NC}" >&2
    fi

    echo -e "${GREEN}=== Update process completed ===${NC}"
    echo -e "${GREEN}Vintage Story server update process finished for version ${NEW_VERSION}${NC}"
    if [ -n "${BACKUP_FILE}" ]; then # Check if BACKUP_FILE variable holds a path (i.e., backup ran successfully)
        echo -e "${CYAN}Backup created at: ${BACKUP_FILE}${NC}"
    elif [ "${SKIP_BACKUP}" = "false" ] && [ "${IGNORE_BACKUP_FAILURE}" = "true" ]; then
        # Remind user if backup was attempted but failed and was ignored
        echo -e "${YELLOW}Reminder: Backup creation was attempted but failed (failure was ignored).${NC}" >&2
    fi

    # Success: cleanup trap will run next with exit code 0
}

# --- Main Execution ---

# Process global options first
while [ "$#" -gt 0 ]; do
    case "$1" in
    --dry-run)
        DRY_RUN=true
        shift
        ;;
    --generate-config)
        # Create a sample configuration file
        CONFIG_SAMPLE="vs_manage.conf.sample"
        echo "# Vintage Story Server Management Script - Configuration File" >"${CONFIG_SAMPLE}"
        echo "# Copy this file to one of the following locations:" >>"${CONFIG_SAMPLE}"
        echo "#   ./vs_manage.conf" >>"${CONFIG_SAMPLE}"
        echo "#   ~/.config/vs_manage/config" >>"${CONFIG_SAMPLE}"
        echo "#   /etc/vs_manage.conf" >>"${CONFIG_SAMPLE}"
        echo "" >>"${CONFIG_SAMPLE}"
        echo "# Service Configuration" >>"${CONFIG_SAMPLE}"
        echo "SERVICE_NAME=\"${SERVICE_NAME}\"" >>"${CONFIG_SAMPLE}"
        echo "" >>"${CONFIG_SAMPLE}"
        echo "# Directory Configuration" >>"${CONFIG_SAMPLE}"
        echo "SERVER_DIR=\"${SERVER_DIR}\"" >>"${CONFIG_SAMPLE}"
        echo "DATA_DIR=\"${DATA_DIR}\"" >>"${CONFIG_SAMPLE}"
        echo "TMP_DIR=\"${TMP_DIR}\"" >>"${CONFIG_SAMPLE}"
        echo "BACKUP_DIR=\"${BACKUP_DIR}\"" >>"${CONFIG_SAMPLE}"
        echo "LOG_DIR=\"${LOG_DIR}\"" >>"${CONFIG_SAMPLE}"
        echo "" >>"${CONFIG_SAMPLE}"
        echo "# User Configuration" >>"${CONFIG_SAMPLE}"
        echo "SERVER_USER=\"${SERVER_USER}\"" >>"${CONFIG_SAMPLE}"
        echo "" >>"${CONFIG_SAMPLE}"
        echo "# Backup Configuration" >>"${CONFIG_SAMPLE}"
        echo "MAX_BACKUPS=${MAX_BACKUPS}" >>"${CONFIG_SAMPLE}"
        echo "" >>"${CONFIG_SAMPLE}"
        echo "# Version Check Configuration" >>"${CONFIG_SAMPLE}"
        echo "VERSION_CHECK_URL=\"${VERSION_CHECK_URL}\"" >>"${CONFIG_SAMPLE}"
        echo "API_VERSION_CHECK_ENABLED=${API_VERSION_CHECK_ENABLED}" >>"${CONFIG_SAMPLE}"
        echo "" >>"${CONFIG_SAMPLE}"

        echo -e "${GREEN}Sample configuration file created: ${CONFIG_SAMPLE}${NC}"
        echo -e "${CYAN}You can copy this file to one of the supported locations and modify it as needed.${NC}"
        exit 0
        ;;
    *)
        break
        ;; # Exit the loop when we hit first positional arg or unknown arg
    esac
done

if [ "$#" -eq 0 ]; then
    show_usage
    exit 0 # Show usage and exit cleanly if no command is given
fi

# Setup cleanup trap *after* initial argument check, so cleanup doesn't run if usage is shown.
# The trap calls the cleanup function with the script's exit status ($?) on EXIT, INT (Ctrl+C), TERM signals.
trap 'cleanup $?' INT TERM EXIT

check_root         # Determine if we're running as root
load_config        # Load configuration from file or environment variables
check_dependencies # Verify required tools are present before proceeding

# Initialize log directory
if [ "${DRY_RUN}" = "false" ]; then
    if [ ! -d "${LOG_DIR}" ]; then
        mkdir -p "${LOG_DIR}" 2>/dev/null || run_with_sudo mkdir -p "${LOG_DIR}"
    fi
    log_message "INFO" "===== vs_manage.sh started - $(date) ====="
else
    echo -e "${BLUE}[DRY RUN] Would initialize log directory: ${LOG_DIR}${NC}" >&2
fi

COMMAND="$1"
shift # Remove command name from argument list ($@)

case "${COMMAND}" in
update) cmd_update "$@" ;;
info) cmd_info "$@" ;;
check-version) cmd_check_version "$@" ;;
# Add new command handlers here
*)
    echo -e "${RED}Unknown command: ${COMMAND}${NC}" >&2
    show_usage
    exit 1
    ;; # Exit with error for unknown command (trap will call cleanup)
esac

# Explicitly disable trap and call cleanup for a normal, successful exit.
# This prevents the trap from firing again unnecessarily. Cleanup exits with 0.
trap - INT TERM EXIT
cleanup 0
