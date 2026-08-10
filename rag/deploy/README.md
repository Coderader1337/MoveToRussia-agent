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
| `RAG_VPS_SSH_KEY` | private key from `rag/deploy/github_actions_deploy_key` (local, gitignored) |

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
