# mntchocoluvr Pebble Deployments

This repository is the PebbleHost-facing deployment mirror for the Discord bots that run on the same PebbleHost instance.

Its job is simple:

- hold only deployment-safe bot files
- keep live secrets and private data out of Git
- provide the Git repository watched by PebbleHost
- keep every bot at the same hierarchy under `Bots/`

## Runtime Layout

- `bot.py` is the only PebbleHost start file. It is a multi-bot manager, not an individual bot.
- `bot-manager.cfg` is the bot registry. Change `enabled = true` or `enabled = false` to start or skip a registered bot.
- `Bots/youtool1-highgrounds/` runs internal bot `youtool1`.
- `Bots/mech1-highgrounds/` runs internal bot `mech1`.
- `Bots/uwu1-tondonights/` runs internal bot `uwu1`.
- Each bot keeps its own `config/`, `data/`, `logs/`, `exports/`, and `import/` directory.
- Each bot must use a different Discord token.

Root-level files are deployment management files only: `bot.py`, `bot-manager.cfg`, `requirements.txt`, `run-pebble.sh`, and documentation.

## Relationship To The Other Repos

- `YOUTOOL1-highgrounds` private source repo: `master` is the stable source-of-truth branch.
- `YOUTOOL1-highgrounds` private working branch: `develop` is for in-progress changes before they are promoted to `master`.
- `YOUTOOL1-highgrounds` source is mirrored into `Bots/youtool1-highgrounds/`.
- `MECH1-highgrounds` source is mirrored into `Bots/mech1-highgrounds/`.
- `UWU1-tondonights` source is mirrored into `Bots/uwu1-tondonights/`.
- This repo: `main` is the deployment branch the remote PebbleHost bot pulls on restart.
- Legacy reference branch: `legacy-rust` preserves the outdated original Rust implementation.

## Bot Manager Config

Edit `bot-manager.cfg` to toggle bots:

```ini
[bot:youtool1]
enabled = true
name = youtool1
path = Bots/youtool1-highgrounds
module = yt_assist
stop_file = data/youtool1.stop
```

Use `enabled = false` to leave a bot registered but skip it on startup. `path` is relative to the deployment root. `stop_file` is relative to the bot folder unless an absolute path is provided.

Internal IDs should stay short and numbered by bot family, such as `mech1`, `uwu1`, `youtool1`, then `mech2`, `uwu2`, or `youtool2` for future siblings. Deployment folder names should append the target server, such as `mech1-highgrounds`, `uwu1-tondonights`, or `youtool1-highgrounds`.

Run this before pushing if you changed layout or toggles:

```powershell
python bot.py --check
```

## What Belongs Here

Include:

- `bot-manager.cfg`
- `Bots/*/bot.py`
- `Bots/*/src/`
- `Bots/*/migrations/`
- `Bots/*/config/*.example`
- safe shared config assets such as catalogs, packages, contracts, templates, and remit item defaults
- `requirements.txt`

Do not include:

- `Bots/*/config/app.toml`
- `.env*`
- `Bots/*/data/`
- `Bots/*/logs/`
- `Bots/*/exports/`
- `Bots/*/import/`
- private tokens, credentials, workstation-only artifacts, or live databases

## Deployment Flow

1. Make and test changes in the private source repo.
2. Promote stable changes into that private repo's stable branch.
3. Mirror only deployment-safe files into this repo under the matching `Bots/<bot>/` folder.
4. Update `bot-manager.cfg` if a bot is added, removed, renamed, or toggled.
5. Run `python bot.py --check`.
6. Push this repo's `main` branch.
7. Restart the PebbleHost bot so Git Management pulls the latest deployment snapshot.

## PebbleHost Settings

- Keep the Python start file set to `bot.py`.
- Keep the Git branch set to `main`.
- Store live tokens only in PebbleHost runtime config files or environment variables.
- If using config files, create each bot's `config/app.toml` from its `config/app.toml.example`.
- If using environment variables, set `YT_ASSIST_DISCORD_TOKEN` for `YOUTOOL1-highgrounds`, `BAKUNAWA_MECH_DISCORD_TOKEN` for `MECH1-highgrounds`, and `UWU_CAFE_DISCORD_TOKEN` for `UWU1-tondonights`.
