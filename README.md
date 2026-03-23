# Twitter/X to Telegram Notifier

Single-process app with internal `collector` and `notifier` modules.

## Features

- One process, clean module boundaries
- Collector/Notifier decoupled by queue interface
- Tweet dedup persistence to avoid duplicate pushes after restart
- Telegram retry with exponential backoff
- Provider switch: `twikit` (real) or `mock` (local test)

## Quick Start (CLI)

1. Create virtual env and install dependencies:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

2. Copy environment template:

```powershell
copy env.example .env
```

3. Set environment values in current shell (or via system env):

```powershell
$env:TWITTER_PROVIDER="mock"
$env:MONITOR_TARGETS='[{"username":"elonmusk","enabled":true}]'
$env:DRY_RUN="true"
```

4. Run once (smoke check):

```powershell
python -m src.main --once
```

5. Run continuously:

```powershell
python -m src.main
```

## Web UI

Run local web console:

```powershell
python -m src.web_main
```

Then open:

`http://127.0.0.1:8000`

You can configure values, start/stop runtime, run test send, and view recent logs.

### Windows one-click scripts

- `setup.bat`: 环境准备（创建 `.venv`、安装依赖），首次使用或依赖变更时双击运行一次即可
- `start_web.bat`: 一键启动 Web UI（需先运行过 `setup.bat`）
- `setup.bat` will also pre-create `.twikit_cookies.json` as a placeholder; real cookies are written after a successful twikit login.

## Environment Notes

- For local testing, set `TWITTER_PROVIDER=mock`.
- Monitor targets now use `MONITOR_TARGETS` JSON, for example `[{\"username\":\"elonmusk\",\"enabled\":true}]`.
- For real collection, set `TWITTER_PROVIDER=twikit` and provide Twikit login fields.
- If Telegram token/chat is missing, notifier logs a warning and skips send.

