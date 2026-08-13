#!/usr/bin/env python3
"""Exchange Yandex OAuth verification code for access_token (one-time setup)."""

import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

CLIENT_ID = os.getenv("YANDEX_OAUTH_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("YANDEX_OAUTH_CLIENT_SECRET", "")
REDIRECT_URI = "https://oauth.yandex.ru/verification_code"

if not CLIENT_ID or not CLIENT_SECRET:
    print("Set YANDEX_OAUTH_CLIENT_ID and YANDEX_OAUTH_CLIENT_SECRET in rag/.env")
    sys.exit(1)

url = (
    "https://oauth.yandex.ru/authorize"
    f"?response_type=code&client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}"
)
print(url)
code = input("\nPaste verification code: ").strip()

r = requests.post(
    "https://oauth.yandex.ru/token",
    data={
        "grant_type": "authorization_code",
        "code": code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    },
    timeout=30,
)
data = r.json()
if r.ok and "access_token" in data:
    print("\nYANDEX_DISK_TOKEN=" + data["access_token"])
else:
    print(data, file=sys.stderr)
    sys.exit(1)
