# Agent Notes

This repository is the private development source for UWU Cafe Bot.

## Deployment Flow

The live watched deployment repository is:

- Local path: `D:\Coding Projects\youtool-pebble-deployment`
- GitHub: `canikou/youtool-pebble-deployment`
- Deployment subdirectory: TBD after private UWU Cafe testing.

When the user asks to work on "UWU Cafe Bot", make code changes here first:

- Local path: `D:\Coding Projects\uwu-cafe-bot`
- GitHub: `canikou/uwu-cafe-bot`

After changes are tested and ready for deployment, mirror deployment-safe files into the watched deployment repo using the same multi-bot runner layout already used there.

Then commit and push both repositories when appropriate:

1. Commit and push this private source repo.
2. Review `D:\Coding Projects\youtool-pebble-deployment` status.
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

Use UV for local setup.

```powershell
python -m pip install --user uv
$uv = "$env:APPDATA\Python\Python314\Scripts\uv.exe"
& $uv sync
```

Run compile smoke checks with:

```powershell
.\.venv\Scripts\python.exe -m compileall -q .
```
