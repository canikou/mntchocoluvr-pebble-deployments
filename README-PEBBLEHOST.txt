PebbleHost upload package for YOUTOOL1-highgrounds, MECH1-highgrounds, and UWU1-tondonights.

This folder is prepared to match PebbleHost's Python bot layout:
- requirements.txt at the root
- bot.py as the required start file
- bot-manager.cfg as the tracked default multi-bot registry
- optional bot-manager.local.cfg as the live enabled/disabled override file

Recommended upload/use:
1. Open PebbleHost File Manager for the bot.
2. Delete the default root requirements.txt and bot.py if PebbleHost created them.
3. Upload the contents of this folder, or upload the zip made from this folder.
4. In PebbleHost Loader, set Bot Start File to bot.py.
5. Add all runtime config files on PebbleHost:
   - Bots/youtool1-highgrounds/config/app.toml for youtool1
   - Bots/mech1-highgrounds/config/app.toml for mech1
   - Bots/uwu1-tondonights/config/app.toml for uwu1
6. Start the bot and watch Console/Chat for startup logs.

The root bot.py starts every enabled bot in bot-manager.cfg as a separate Python process:
- youtool1 runs from Bots/youtool1-highgrounds.
- mech1 runs from Bots/mech1-highgrounds.
- uwu1 runs from Bots/uwu1-tondonights.

To quickly disable a bot in Git, edit bot-manager.cfg and set its section to enabled = false.
To make live PebbleHost toggles survive Git refreshes, copy bot-manager.cfg to bot-manager.local.cfg on the host and edit bot-manager.local.cfg instead.
Leave sections registered so bots can be re-enabled later without changing launcher code.

Each bot keeps its own config, data, logs, exports, imports, database, stop file, and Discord token.
Do not reuse the same token in multiple config files.

If you prefer environment variables instead of writing tokens into app.toml, use:
- YT_ASSIST_DISCORD_TOKEN for YOUTOOL1-highgrounds
- BAKUNAWA_MECH_DISCORD_TOKEN for MECH1-highgrounds
- UWU_CAFE_DISCORD_TOKEN for UWU1-tondonights

Included runtime data:
- bot-manager.cfg
- Bots/youtool1-highgrounds/config/ with youtool1 content files and app.toml.example
- Bots/mech1-highgrounds/config/ with mech1 content files and app.toml.example
- Bots/uwu1-tondonights/config/ with uwu1 content files and app.toml.example

Intentionally omitted:
- Bots/youtool1-highgrounds/config/app.toml
- Bots/mech1-highgrounds/config/app.toml
- Bots/uwu1-tondonights/config/app.toml
- Bots/*/data/
- Bots/*/logs/
- Bots/*/exports/
- Bots/*/import/
- private tokens, credentials, and live databases

The launcher creates data/, logs/, exports/, and import/ directories inside each enabled bot folder when it starts.
