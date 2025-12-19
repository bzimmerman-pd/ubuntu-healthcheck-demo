#!/usr/bin/env bash
set -euo pipefail

# Ubuntu Health Check
# - Outputs both human-readable and JSON (toggle with --format)
# - Exits non-zero if any critical checks fail (great for CI and Runbook gating)

DISK_THRESHOLD=${DISK_THRESHOLD:-85}
PING_TARGET=${PING_TARGET:-8.8.8.8}
FORMAT=${FORMAT:-both} # both|json|markdown|text
CHECK_SERVICES=${CHECK_SERVICES:-"ssh systemd-journald cron"}

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --disk-threshold) DISK_THRESHOLD="$2"; shift 2 ;;
    --ping) PING_TARGET="$2"; shift 2 ;;
    --services) CHECK_SERVICES="$2"; shift 2 ;;
    --format) FORMAT="$2"; shift 2 ;;
    --json) FORMAT="json"; shift 1 ;;
    --markdown) FORMAT="markdown"; shift 1 ;;
    --text) FORMAT="text"; shift 1 ;;
    -h|--help)
      echo "Usage: $0 [--disk-threshold PCT] [--ping HOST] [--services \"svc1 svc2\"] [--format both|json|markdown|text]"
      exit 0
      ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Helpers
has_cmd() { command -v "$1" >/dev/null 2>&1; }
json_escape() { jq -Rsa . <<< "$1"; } # requires jq
now_iso() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

require_root_checks=false
fail_reasons=()

# Collect facts
HOSTNAME=$(hostname)
KERNEL=$(uname -r)
OS_NAME=$(grep -E '^PRETTY_NAME=' /etc/os-release | cut -d= -f2 | tr -d '"')
UPTIME=$(uptime -p || true)
BOOT_TIME=$(who -b | awk '{print $3" "$4}' || true)

# Load averages
LOAD_AVG_1=$(cut -d' ' -f1 < /proc/loadavg)
LOAD_AVG_5=$(cut -d' ' -f2 < /proc/loadavg)
LOAD_AVG_15=$(cut -d' ' -f3 < /proc/loadavg)

# CPU usage (approx)
CPU_USAGE=""
if has_cmd mpstat; then
  CPU_USAGE=$(mpstat 1 1 | awk '/Average/ && $3 ~ /CPU/ {next} /Average/ {print 100 - $NF}')
elif has_cmd top; then
  CPU_USAGE=$(top -bn1 | awk -F'[, ]+' '/%Cpu/ {print 100-$8}')
fi

# Memory
MEM_TOTAL=$(grep MemTotal /proc/meminfo | awk '{print $2}')
MEM_AVAILABLE=$(grep MemAvailable /proc/meminfo | awk '{print $2}')
MEM_USED=$((MEM_TOTAL - MEM_AVAILABLE))
MEM_USED_PCT=$(awk -v u="$MEM_USED" -v t="$MEM_TOTAL" 'BEGIN{printf "%.1f", (u/t)*100}')

# Disk root
read -r DISK_FS DISK_SIZE DISK_USED DISK_AVAIL DISK_USE_PCT DISK_MOUNT <<< "$(df -hP / | awk 'NR==2{print $1,$2,$3,$4,$5,$6}')"
DISK_USE_NUM=${DISK_USE_PCT%%%}

# Packages needing upgrades
PKG_UPGRADES=""
if has_cmd apt-get; then
  if sudo -n true 2>/dev/null; then
    PKG_UPGRADES=$(apt-get -s upgrade | awk '/^Inst /{count++} END{print count+0}')
  else
    PKG_UPGRADES=$(apt-get -s upgrade 2>/dev/null | awk '/^Inst /{count++} END{print count+0}')
  fi
fi

# Docker status (if installed)
DOCKER_STATUS=""
if has_cmd docker; then
  if docker info >/dev/null 2>&1; then
    DOCKER_STATUS="running"
  else
    DOCKER_STATUS="installed_not_running_or_no_perms"
  fi
fi

# Ping check
PING_OK="unknown"
if has_cmd ping; then
  if ping -c 1 -W 2 "$PING_TARGET" >/dev/null 2>&1; then
    PING_OK="true"
  else
    PING_OK="false"
    fail_reasons+=("Ping to $PING_TARGET failed")
  fi
fi

# Services check
declare -a SERVICES_DOWN=()
if has_cmd systemctl; then
  for svc in $CHECK_SERVICES; do
    if systemctl is-active --quiet "$svc"; then
      : # ok
    else
      SERVICES_DOWN+=("$svc")
    fi
  done
fi
if (( ${#SERVICES_DOWN[@]} > 0 )); then
  fail_reasons+=("Services not active: ${SERVICES_DOWN[*]}")
fi

# Disk threshold check
if [[ -n "$DISK_USE_NUM" ]] && (( DISK_USE_NUM > DISK_THRESHOLD )); then
  fail_reasons+=("Root disk usage ${DISK_USE_PCT} exceeds ${DISK_THRESHOLD}%")
fi

# Health status
STATUS="healthy"
if (( ${#fail_reasons[@]} > 0 )); then
  STATUS="unhealthy"
fi

# Build outputs
SUMMARY_TEXT=$(cat <<EOF
Ubuntu Health Check - $(now_iso)
Host: ${HOSTNAME}
OS: ${OS_NAME}
Kernel: ${KERNEL}
Uptime: ${UPTIME} (boot: ${BOOT_TIME})

CPU load: 1m=${LOAD_AVG_1} 5m=${LOAD_AVG_5} 15m=${LOAD_AVG_15}
CPU usage: ${CPU_USAGE:-unknown}%

Memory: used=${MEM_USED_PCT}% (kB used=${MEM_USED}, total=${MEM_TOTAL})
Disk (/): ${DISK_USE_PCT} used (size=${DISK_SIZE}, avail=${DISK_AVAIL})

Ping ${PING_TARGET}: ${PING_OK}
Services checked: ${CHECK_SERVICES}
Services down: ${SERVICES_DOWN[*]:-none}
Packages pending upgrades: ${PKG_UPGRADES:-unknown}
Docker: ${DOCKER_STATUS:-not_installed}

Status: ${STATUS}
EOF
)

SUMMARY_MD=$(cat <<'EOF'
#### Ubuntu Health Check

- Status: STATUS_VAL
- Host: HOST_VAL
- OS: OS_VAL
- Kernel: KERNEL_VAL
- Uptime: UPTIME_VAL (boot: BOOT_VAL)

- CPU load:
  - 1m: L1
  - 5m: L5
  - 15m: L15
- CPU usage: CPUU%

- Memory
  - Used: MEMUSED% (kB used=MEMUSEDKB, total=MEMTOTKB)
- Disk (/)
  - Usage: DISKPCT (size=DISKSZ, avail=DISKAVL)

- Network
  - Ping PINGHOST: PINGOK

- Services
  - Checked: SRVCHK
  - Down: SRVDOWN

- Packages pending upgrades: PKGUP
- Docker: DOCKERSTAT
EOF
)

SUMMARY_MD=${SUMMARY_MD//STATUS_VAL/$STATUS}
SUMMARY_MD=${SUMMARY_MD//HOST_VAL/$HOSTNAME}
SUMMARY_MD=${SUMMARY_MD//OS_VAL/$OS_NAME}
SUMMARY_MD=${SUMMARY_MD//KERNEL_VAL/$KERNEL}
SUMMARY_MD=${SUMMARY_MD//UPTIME_VAL/$UPTIME}
SUMMARY_MD=${SUMMARY_MD//BOOT_VAL/$BOOT_TIME}
SUMMARY_MD=${SUMMARY_MD//L1/$LOAD_AVG_1}
SUMMARY_MD=${SUMMARY_MD//L5/$LOAD_AVG_5}
SUMMARY_MD=${SUMMARY_MD//L15/$LOAD_AVG_15}
SUMMARY_MD=${SUMMARY_MD//CPUU/${CPU_USAGE:-unknown}}
SUMMARY_MD=${SUMMARY_MD//MEMUSED/$MEM_USED_PCT}
SUMMARY_MD=${SUMMARY_MD//MEMUSEDKB/$MEM_USED}
SUMMARY_MD=${SUMMARY_MD//MEMTOTKB/$MEM_TOTAL}
SUMMARY_MD=${SUMMARY_MD//DISKPCT/$DISK_USE_PCT}
SUMMARY_MD=${SUMMARY_MD//DISKSZ/$DISK_SIZE}
SUMMARY_MD=${SUMMARY_MD//DISKAVL/$DISK_AVAIL}
SUMMARY_MD=${SUMMARY_MD//PINGHOST/$PING_TARGET}
SUMMARY_MD=${SUMMARY_MD//PINGOK/$PING_OK}
SUMMARY_MD=${SUMMARY_MD//SRVCHK/$CHECK_SERVICES}
SUMMARY_MD=${SUMMARY_MD//SRVDOWN/${SERVICES_DOWN[*]:-none}}
SUMMARY_MD=${SUMMARY_MD//PKGUP/${PKG_UPGRADES:-unknown}}
SUMMARY_MD=${SUMMARY_MD//DOCKERSTAT/${DOCKER_STATUS:-not_installed}}

# JSON (requires jq)
JSON="{}"
if has_cmd jq; then
  JSON=$(jq -n \
    --arg now "$(now_iso)" \
    --arg hostname "$HOSTNAME" \
    --arg os "$OS_NAME" \
    --arg kernel "$KERNEL" \
    --arg uptime "$UPTIME" \
    --arg boot "$BOOT_TIME" \
    --arg l1 "$LOAD_AVG_1" --arg l5 "$LOAD_AVG_5" --arg l15 "$LOAD_AVG_15" \
    --arg cpu_usage "${CPU_USAGE:-}" \
    --arg mem_used_pct "$MEM_USED_PCT" \
    --arg mem_used_kb "$MEM_USED" \
    --arg mem_total_kb "$MEM_TOTAL" \
    --arg disk_pct "$DISK_USE_PCT" \
    --arg disk_size "$DISK_SIZE" \
    --arg disk_avail "$DISK_AVAIL" \
    --arg ping_host "$PING_TARGET" \
    --arg ping_ok "$PING_OK" \
    --arg services "$CHECK_SERVICES" \
    --arg services_down "${SERVICES_DOWN[*]}" \
    --arg pkg_upgrades "${PKG_UPGRADES:-}" \
    --arg docker "${DOCKER_STATUS:-}" \
    --arg status "$STATUS" \
    --arg disk_threshold "$DISK_THRESHOLD" \
    --argjson failures "$(printf '%s\n' "${fail_reasons[@]:-}" | jq -R . | jq -s .)" \
    '{
      timestamp: $now,
      host: $hostname,
      os: $os,
      kernel: $kernel,
      uptime: $uptime,
      boot_time: $boot,
      loadavg: { "1m": $l1, "5m": $l5, "15m": $l15 },
      cpu_usage_pct: $cpu_usage,
      memory: { used_pct: $mem_used_pct, used_kb: $mem_used_kb, total_kb: $mem_total_kb },
      disk_root: { used_pct: $disk_pct, size: $disk_size, avail: $disk_avail, threshold_pct: $disk_threshold },
      network: { ping_host: $ping_host, ping_ok: $ping_ok },
      services: { checked: ($services|split(" ")), down: ($services_down|split(" ")|map(select(. != ""))) },
      packages: { upgrades_pending: $pkg_upgrades },
      docker: $docker,
      status: $status,
      failures: $failures
    }')
fi

# Output selection
case "$FORMAT" in
  json) echo "$JSON" ;;
  markdown) echo "$SUMMARY_MD" ;;
  text) echo "$SUMMARY_TEXT" ;;
  both)
    echo "$SUMMARY_MD"
    echo
    echo '```json'
    echo "$JSON"
    echo '```'
    ;;
  *) echo "$SUMMARY_TEXT" ;;
esac

# Exit code
if [[ "$STATUS" != "healthy" ]]; then
  exit 1
fi
