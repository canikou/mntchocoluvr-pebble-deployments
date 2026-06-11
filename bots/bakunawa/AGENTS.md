# Agent Notes

This repository is the private development source for Bakunawa Mech Bot.

## Deployment Flow

The live watched deployment repository is:

- Local path: this deployment repository root
- GitHub: `canikou/youtool-pebble-deployment`
- Deployment subdirectory: `bots/bakunawa/`

When the user asks to work on "Bakunawa Mech Bot", make code changes here first:

- Local path: `..\..\bakunawa-mech-bot` from the deployment repo root
- GitHub: `canikou/bakunawa-mech-bot`

After changes are tested and ready for deployment, mirror deployment-safe files into the watched deployment repo:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\mirror-to-deployment.ps1
```

Then commit and push both repositories when appropriate:

1. Commit and push this private source repo.
2. Review this deployment repository status.
3. Commit and push `youtool-pebble-deployment` `main` so the remote host can pick up the update.

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
