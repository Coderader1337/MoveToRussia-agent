#!/usr/bin/env bash
# Print a short summary from collected VPS stats CSV files.
set -euo pipefail

MONITOR_DIR="${MONITOR_DIR:-/opt/movetorussia/monitoring}"
DATA_DIR="$MONITOR_DIR/data"
DAYS="${1:-1}"

if [[ ! -d "$DATA_DIR" ]]; then
  echo "No stats directory: $DATA_DIR" >&2
  exit 1
fi

mapfile -t FILES < <(find "$DATA_DIR" -type f -name 'stats-*.csv' -mtime -"$DAYS" | sort)
if [[ "${#FILES[@]}" -eq 0 ]]; then
  echo "No stats files for the last ${DAYS} day(s) in $DATA_DIR"
  exit 0
fi

python3 - "$DAYS" "${FILES[@]}" <<'PY'
import csv
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

days = int(sys.argv[1])
files = sys.argv[2:]

rows = []
for path in files:
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rows.append(row)

if not rows:
    print(f"No samples in the last {days} day(s).")
    raise SystemExit(0)

def fnum(value):
    value = (value or "").strip()
    if not value:
        return None
    return float(value)

def summarize(values):
    if not values:
        return None
    values = sorted(values)
    p95 = values[max(0, int(len(values) * 0.95) - 1)]
    return {
        "n": len(values),
        "min": min(values),
        "avg": statistics.mean(values),
        "p95": p95,
        "max": max(values),
    }

def fmt(summary):
    if not summary:
        return "n/a"
    return (
        f"n={summary['n']} "
        f"min={summary['min']:.2f} "
        f"avg={summary['avg']:.2f} "
        f"p95={summary['p95']:.2f} "
        f"max={summary['max']:.2f}"
    )

first_ts = rows[0]["timestamp"]
last_ts = rows[-1]["timestamp"]
print(f"=== VPS stats summary (last {days} day(s)) ===")
print(f"Files: {len(files)}")
print(f"Samples: {len(rows)}")
print(f"From: {first_ts}")
print(f"To:   {last_ts}")
print()

system_cpu_proxy = [fnum(r["load1"]) for r in rows if fnum(r["load1"]) is not None]
mem_used = [fnum(r["mem_used_mb"]) for r in rows if fnum(r["mem_used_mb"]) is not None]
mem_avail = [fnum(r["mem_avail_mb"]) for r in rows if fnum(r["mem_avail_mb"]) is not None]
disk_used = [fnum(r["disk_used_pct"]) for r in rows if fnum(r["disk_used_pct"]) is not None]

print("System:")
print(f"  load1:        {fmt(summarize(system_cpu_proxy))}")
print(f"  mem_used_mb:  {fmt(summarize(mem_used))}")
print(f"  mem_avail_mb: {fmt(summarize(mem_avail))}")
print(f"  disk_used_%:  {fmt(summarize(disk_used))}")
print()

by_container = defaultdict(lambda: {"cpu": [], "mem_mib": [], "mem_pct": [], "status": defaultdict(int)})
for row in rows:
    name = row["container"]
    by_container[name]["status"][row.get("container_status", "")] += 1
    cpu = fnum(row.get("cpu_pct"))
    mem_mib = fnum(row.get("mem_mib"))
    mem_pct = fnum(row.get("mem_pct"))
    if cpu is not None:
        by_container[name]["cpu"].append(cpu)
    if mem_mib is not None:
        by_container[name]["mem_mib"].append(mem_mib)
    if mem_pct is not None:
        by_container[name]["mem_pct"].append(mem_pct)

for name in sorted(by_container):
    data = by_container[name]
    print(name + ":")
    statuses = ", ".join(f"{k}={v}" for k, v in sorted(data["status"].items()) if k)
    print(f"  statuses: {statuses or 'n/a'}")
    print(f"  cpu_%:    {fmt(summarize(data['cpu']))}")
    print(f"  mem_mib:  {fmt(summarize(data['mem_mib']))}")
    print(f"  mem_%:    {fmt(summarize(data['mem_pct']))}")
    print()
PY
