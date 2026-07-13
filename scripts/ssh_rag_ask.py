"""Run a RAG question on the VPS over SSH and print the answer.

The script connects to the remote machine, runs the existing
`rag_console_test.py` helper there, and prints only the final answer.

Usage:
  python scripts/ssh_rag_ask.py
  python scripts/ssh_rag_ask.py "How long does the TRP process take?"

Recommended env vars:
  SSH_HOST=207.244.254.188
  SSH_USER=root
  SSH_PASSWORD=...
  SSH_RAG_DIR=/root/mail_agent/rag
"""

from __future__ import annotations

import argparse
import getpass
import os
import shlex
import sys
from pathlib import Path

import paramiko


DEFAULT_QUESTION = "Что нужно, чтобы получить shared values visa?"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="?", default=DEFAULT_QUESTION)
    parser.add_argument("--host", default=_env("SSH_HOST", "207.244.254.188"))
    parser.add_argument("--user", default=_env("SSH_USER", "root"))
    parser.add_argument("--password", default=_env("SSH_PASSWORD", ""))
    parser.add_argument("--remote-dir", default=_env("SSH_RAG_DIR", "/root/mail_agent/rag"))
    parser.add_argument(
        "--full-output",
        action="store_true",
        help="Print the full remote output instead of only the final answer.",
    )
    return parser.parse_args()


def run_remote_question(
    host: str,
    user: str,
    password: str,
    remote_dir: str,
    question: str,
) -> str:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        username=user,
        password="Og5bENdxXNFyd4eDs2knF0xOsXZ9",
        timeout=20,
        banner_timeout=20,
        auth_timeout=20,
        look_for_keys=False,
        allow_agent=False,
    )

    remote_question = shlex.quote(question)
    remote_cmd = (
        f"cd {shlex.quote(remote_dir)} "
        f"&& python3 ../scripts/rag_console_test.py {remote_question}"
    )
    stdin, stdout, stderr = client.exec_command(remote_cmd, timeout=600)
    exit_code = stdout.channel.recv_exit_status()
    output = stdout.read().decode("utf-8", errors="replace")
    error = stderr.read().decode("utf-8", errors="replace")
    client.close()

    if exit_code != 0:
        raise RuntimeError(
            f"Remote command failed with exit code {exit_code}.\n"
            f"STDOUT:\n{output}\nSTDERR:\n{error}"
        )

    return output.strip()


def extract_answer(remote_output: str) -> str:
    marker = "=== DeepSeek answer ==="
    if marker not in remote_output:
        return remote_output.strip()

    answer = remote_output.split(marker, 1)[1].strip()
    return answer or remote_output.strip()


def main() -> int:
    args = parse_args()
    password = args.password or getpass.getpass(
        f"SSH password for {args.user}@{args.host}: "
    )

    try:
        remote_output = run_remote_question(
            host=args.host,
            user=args.user,
            password=password,
            remote_dir=args.remote_dir,
            question=args.question,
        )
    except Exception as exc:  # noqa: BLE001
        print(str(exc), file=sys.stderr)
        return 1

    if args.full_output:
        print(remote_output)
    else:
        print(extract_answer(remote_output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
