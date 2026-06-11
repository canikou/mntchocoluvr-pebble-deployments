# Agent Notes

This directory is the PebbleHost-ready deployment copy for internal bot `mech1`.

## Deployment Mapping

- Deployment repo: `D:\! Coding Projects\! Deployments\mntchocoluvr-pebble-deployments`
- Deployment subdirectory: `Bots/mech1-highgrounds/`
- Source workspace: `D:\! Coding Projects\! Bots\MECH1-highgrounds`
- GitHub deployment repo: `canikou/mntchocoluvr-pebble-deployments`
- GitHub source repo: `canikou/MECH1-highgrounds`

Root `bot.py` starts this bot through `bot-manager.cfg`. Toggle this bot by changing `[bot:mech1] enabled`.

## Mirror Rules

Mirror deployment-safe source files into this directory when Bakunawa changes are ready for PebbleHost:

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
