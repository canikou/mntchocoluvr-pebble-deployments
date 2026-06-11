# Agent Notes

This directory is the PebbleHost-ready deployment copy for UWU1-tondonights.

## Deployment Flow

The live watched deployment repository is:

- Local path: this deployment repository root
- GitHub: `canikou/mntchocoluvr-pebble-deployments`
- Deployment subdirectory: TBD after private UWU Cafe testing.

When the user asks to work on "UWU Cafe Bot", make code changes here first:

- Local path: `..\..\! Bots\UWU1-tondonights` from the deployment repo root, if that private source repo is cloned locally
- GitHub: `canikou/UWU1-tondonights`

After changes are tested and ready for deployment, mirror deployment-safe files into the watched deployment repo using the same multi-bot runner layout already used there.

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

Use UV for local setup.

```powershell
uv sync
```

Run compile smoke checks with:

```powershell
uv run python -m compileall -q .
```
