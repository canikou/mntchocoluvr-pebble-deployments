# Agent Notes

This directory is the PebbleHost-ready deployment copy for internal bot `famd5`.

## Deployment Mapping

- Deployment repo: the repository root containing this `Bots/` directory.
- Deployment subdirectory: `Bots/famd5-5th/`
- Source workspace: `! Bots/FAMD-5th` in the same workspace root.
- GitHub deployment repo: `canikou/mntchocoluvr-pebble-deployments`
- GitHub source repo: `canikou/FAMD-5th`

Root `bot.py` starts this bot through `bot-manager.cfg`. Toggle this bot by changing `[bot:famd5] enabled`.

## Mirror Rules

Mirror deployment-safe source files into this directory when FAMD changes are ready for PebbleHost:

- `bot.py`
- `src/`
- `migrations/`
- safe files in `config/`
- `AGENTS.md`

Never commit live secrets or runtime data:

- `.env`
- `.env.*`
- `config/app.toml`
- `data/`
- `logs/`
- `exports/`
- `import/`

## Local Setup

Use Python 3.12 or newer for local checks.

```powershell
python -m compileall -q .
```
