"""Deploy VPS monitoring scripts and install a cron job.

Usage:
  python scripts/deploy_vps_monitoring.py
  python scripts/deploy_vps_monitoring.py --run-now
"""

from __future__ import annotations

import argparse
import getpass
import os
import shlex
import sys
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parent.parent
REMOTE_DIR = "/opt/movetorussia/monitoring"
CRON_LINE = "*/5 * * * * /opt/movetorussia/monitoring/collect_stats.sh >> /opt/movetorussia/monitoring/logs/cron.log 2>&1"
FILES = {
    "collect_stats.sh": ROOT / "scripts" / "vps_collect_stats.sh",
    "stats_summary.sh": ROOT / "scripts" / "vps_stats_summary.sh",
}


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=env("SSH_HOST", "207.244.254.188"))
    parser.add_argument("--user", default=env("SSH_USER", "root"))
    parser.add_argument("--password", default=env("SSH_PASSWORD", ""))
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Run one stats collection immediately after deploy.",
    )
    return parser.parse_args()


def run(client: paramiko.SSHClient, command: str) -> tuple[int, str, str]:
    stdin, stdout, stderr = client.exec_command(command, timeout=120)
    code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return code, out, err


def main() -> int:
    args = parse_args()
    password = args.password or getpass.getpass(f"SSH password for {args.user}@{args.host}: ")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=args.host,
        username=args.user,
        password=password,
        timeout=20,
        banner_timeout=20,
        auth_timeout=20,
        look_for_keys=False,
        allow_agent=False,
    )

    sftp = client.open_sftp()
    try:
        run(client, f"mkdir -p {shlex.quote(REMOTE_DIR)}/data {shlex.quote(REMOTE_DIR)}/logs")
        for remote_name, local_path in FILES.items():
            remote_path = f"{REMOTE_DIR}/{remote_name}"
            content = local_path.read_text(encoding="utf-8").replace("\r\n", "\n")
            with sftp.file(remote_path, "w") as remote_file:
                remote_file.write(content)
            run(client, f"chmod +x {shlex.quote(remote_path)}")
            print(f"uploaded: {remote_path}")
    finally:
        sftp.close()

    marker = "# movetorussia-vps-monitoring"
    cron_update = (
        f"(crontab -l 2>/dev/null | grep -Fv {shlex.quote(marker)}; "
        f"echo {shlex.quote(CRON_LINE + ' ' + marker)}) | crontab -"
    )
    code, out, err = run(client, cron_update)
    if code != 0:
        print(err or out, file=sys.stderr)
        client.close()
        return 1
    print("cron installed: every 5 minutes")

    if args.run_now:
        code, out, err = run(client, f"{REMOTE_DIR}/collect_stats.sh")
        print(out.strip())
        if code != 0:
            print(err, file=sys.stderr)
            client.close()
            return code
        code, out, err = run(client, f"{REMOTE_DIR}/stats_summary.sh 1")
        print(out.strip())

    client.close()
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
