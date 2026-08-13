# RAG bot CI/CD

## Instances on VPS

| Environment | Path | systemd service | Branch |
|---|---|---|---|
| Production | `/opt/movetorussia/rag` | `movetorussia-rag-bot` | `master` |
| Dev / test | `/opt/movetorussia/rag-dev` | `movetorussia-rag-bot-dev` | `rag_develop` |

Both instances share the same Qdrant (`localhost:6333`) and API keys. Only `TELEGRAM_BOT_TOKEN` and stats CSV path differ.

## GitHub Actions

Workflows:

- `.github/workflows/rag-deploy-dev.yml` — push to `rag_develop` (paths under `rag/**`)
- `.github/workflows/rag-deploy-prod.yml` — push to `master` after merge from `rag_develop`

### Required repository secrets

Add in GitHub → Settings → Secrets and variables → Actions:

| Secret | Value |
|---|---|
| `RAG_VPS_HOST` | `207.244.254.188` |
| `RAG_VPS_USER` | `root` |
| `RAG_VPS_SSH_KEY` | **private** key from `rag/deploy/github_actions_deploy_key` (local, gitignored) |

Copy the entire file including `-----BEGIN OPENSSH PRIVATE KEY-----` / `-----END...` lines. Do **not** paste the `.pub` file.

Public key is already in `/root/.ssh/authorized_keys` on the VPS (`github-actions-rag-deploy`).

## Manual one-time bootstrap (dev)

```bash
bash rag/deploy/bootstrap-dev-instance.sh '<TELEGRAM_BOT_TOKEN>'
bash rag/deploy/install-and-restart.sh /opt/movetorussia/rag-dev movetorussia-rag-bot-dev
```

## Manual restart

```bash
systemctl restart movetorussia-rag-bot-dev   # test
systemctl restart movetorussia-rag-bot       # prod
```

## Yandex Disk supplemental corpus (daily sync)

Txt files on Yandex Disk are synced into Qdrant as `source=yandex_disk` **without**
touching mailbox/FAQ chunks. Runs once per day at 00:00 MSK on **prod only**
(`/opt/movetorussia/rag`).

### One-time setup

1. Create a Yandex OAuth app at https://oauth.yandex.ru (scope `cloud_api:disk.read`)
2. Add to `/opt/movetorussia/rag/.env`:
   ```
   YANDEX_DISK_TOKEN=<oauth_token>
   YANDEX_DISK_REMOTE_DIR=/rag_corpus
   ```
3. Create folder `/rag_corpus` on Yandex Disk and upload `.txt` files
4. Install the timer (once):
   ```bash
   bash /opt/movetorussia/rag/deploy/bootstrap-disk-sync.sh
   ```

### Manual run / logs

```bash
systemctl start movetorussia-rag-disk-sync.service
journalctl -u movetorussia-rag-disk-sync.service -n 50 --no-pager
systemctl list-timers movetorussia-rag-disk-sync.timer
```

Local data mirror: `/opt/movetorussia/rag/data/yandex_disk/` (not deployed via rsync).

_Last CI deploy check: 2026-08-10 (manual OK, ci pending)_
