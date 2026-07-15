#!/usr/bin/env bash
# Collect VPS resource usage for MoveToRussia projects.
# Intended to run from cron every 5 minutes on 207.244.254.188.
set -euo pipefail

MONITOR_DIR="${MONITOR_DIR:-/opt/movetorussia/monitoring}"
DATA_DIR="$MONITOR_DIR/data"
LOG_DIR="$MONITOR_DIR/logs"
RETENTION_DAYS="${RETENTION_DAYS:-30}"

CONTAINERS=(
  "movetorussia_mail_agent_api"
  "movetorussia_reply_bot"
)

mkdir -p "$DATA_DIR" "$LOG_DIR"

timestamp_utc() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

to_mib() {
  local raw="${1%% *}"
  if [[ "$raw" == *GiB ]]; then
    local num="${raw%GiB}"
    awk -v n="$num" 'BEGIN { printf "%.2f", n * 1024 }'
  elif [[ "$raw" == *MiB ]]; then
    echo "${raw%MiB}"
  elif [[ "$raw" == *KiB ]]; then
    local num="${raw%KiB}"
    awk -v n="$num" 'BEGIN { printf "%.2f", n / 1024 }'
  else
    echo ""
  fi
}

strip_pct() {
  echo "${1%%%}"
}

csv_field() {
  local value="${1:-}"
  value="${value//\"/\"\"}"
  printf '"%s"' "$value"
}

DATE="$(date -u +%Y-%m-%d)"
OUT_FILE="$DATA_DIR/stats-${DATE}.csv"
TS="$(timestamp_utc)"

if [[ ! -f "$OUT_FILE" ]]; then
  cat >"$OUT_FILE" <<'EOF'
timestamp,load1,load5,load15,mem_total_mb,mem_used_mb,mem_avail_mb,disk_used_pct,disk_used_gb,disk_avail_gb,container,container_status,cpu_pct,mem_mib,mem_pct
EOF
fi

read -r LOAD1 LOAD5 LOAD15 _ < /proc/loadavg
read -r MEM_TOTAL MEM_USED MEM_AVAIL < <(free -m | awk '/^Mem:/ { print $2, $3, $7 }')
read -r DISK_USED_PCT DISK_USED_GB DISK_AVAIL_GB < <(df -BG / | awk 'NR==2 { gsub(/G/, "", $3); gsub(/G/, "", $4); print $5, $3, $4 }')

for container in "${CONTAINERS[@]}"; do
  status="missing"
  cpu_pct=""
  mem_mib=""
  mem_pct=""

  if docker inspect "$container" >/dev/null 2>&1; then
    status="$(docker inspect -f '{{.State.Status}}' "$container" 2>/dev/null || echo unknown)"
    if [[ "$status" == "running" ]]; then
      stats_line="$(docker stats --no-stream --format '{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}' "$container" 2>/dev/null || true)"
      if [[ -n "$stats_line" ]]; then
        IFS='|' read -r cpu_raw mem_usage_raw mem_pct_raw <<<"$stats_line"
        cpu_pct="$(strip_pct "$cpu_raw")"
        mem_mib="$(to_mib "$mem_usage_raw")"
        mem_pct="$(strip_pct "$mem_pct_raw")"
      fi
    fi
  fi

  {
    printf '%s,' "$TS"
    printf '%s,' "$LOAD1"
    printf '%s,' "$LOAD5"
    printf '%s,' "$LOAD15"
    printf '%s,' "$MEM_TOTAL"
    printf '%s,' "$MEM_USED"
    printf '%s,' "$MEM_AVAIL"
    printf '%s,' "$DISK_USED_PCT"
    printf '%s,' "$DISK_USED_GB"
    printf '%s,' "$DISK_AVAIL_GB"
    printf '%s,' "$(csv_field "$container")"
    printf '%s,' "$(csv_field "$status")"
    printf '%s,' "$cpu_pct"
    printf '%s,' "$mem_mib"
    printf '%s\n' "$mem_pct"
  } >>"$OUT_FILE"
done

find "$DATA_DIR" -type f -name 'stats-*.csv' -mtime +"$RETENTION_DAYS" -delete
