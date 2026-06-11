# Agent Notes

This directory is the PebbleHost-ready deployment copy for MECH1-highgrounds.

## Deployment Flow

The live watched deployment repository is:

- Local path: this deployment repository root
- GitHub: `canikou/mntchocoluvr-pebble-deployments`
- Deployment subdirectory: `bots/bakunawa/`

When the user asks to work on "Bakunawa Mech Bot", make code changes here first:

- Local path: `..\..\! Bots\MECH1-highgrounds` from the deployment repo root
- GitHub: `canikou/MECH1-highgrounds`

After changes are tested and ready for deployment, mirror deployment-safe files into the watched deployment repo:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\mirror-to-deployment.ps1
```

Then commit and push both repositories when appropriate:

1. Commit and push this private source repo.
2. Review this deployment repository status.
3. Commit and push `mntchocoluvr-pebble-deployments` `main` so the remote host can pick up the update.

Never commit live secrets or runtime data:

- `.env`
- `.env.*`
- `config/app.toml`
- `data/`
- `logs/`
- `exports/`
- `import/`

## Local Setup

Use Python 3.12.

```powershell
uv venv --python 3.13
uv pip install -r requirements.txt
```

Run compile smoke checks with:

```powershell
uv run python -m compileall -q .
```
