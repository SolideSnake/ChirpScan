# ChirpScan 2.0

ChirpScan 是一个本地运行的 X 监控与平台推送工具。2.0 版本把目标管理统一到 `X 监控` 页面，并新增 `币安广场` 发布能力；当前支持把同一条推文按目标规则推送到 `电报`、`飞书` 和 `币安广场`。

## 2.0 主要变化

- 新增币安广场发布模块，支持按目标单独开启或关闭。
- 目标配置统一为 `MONITOR_TARGETS`，一个 X 用户下分别配置电报、飞书和币安广场规则。
- 监控目标新增 `include_replies` 开关，默认只监控主贴；开启后抓取主贴和回复。
- 电报、飞书和币安广场各自拥有独立关键词规则，互不影响。
- 币安广场发布状态会持久化，避免同一条推文重复发文。
- 日志终端改为业务化事件列表，常见 X 请求日志会显示为短句。
- 页面只保留两个一级入口：`X 监控` 和 `设置`。

## 功能概览

- X 采集：默认使用 `twikit` 真实抓取，也保留 `mock` 模式用于本地测试。
- 目标管理：在 `X 监控` 页面新增 X 用户，并为每个目标选择是否监控回复，以及电报、飞书、币安广场或多平台同时推送。
- 关键词规则：支持包含或排除规则，多个规则用英文逗号分隔，组合词可用 `+` 表示同时命中，例如 `launchpool+binance`。
- 自动保存：目标、平台开关和关键词规则修改后会自动保存；运行中的监控需要重启任务后加载新配置。
- 去重存储：采集层用 `.state/dedup.json` 避免同一条推文重复进入处理队列。
- 平台状态：币安广场用 `.state/delivery_status.json` 记录发布结果，防止重复发文。

## 快速启动

推荐直接使用批处理脚本：

```powershell
setup.bat
start_web.bat
```

也可以手动启动：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m src.web_main
```

启动后打开：

```text
http://127.0.0.1:8000
```

## 页面说明

- `X 监控`：新增监控目标，配置目标级平台开关和关键词规则，启动、停止、重启监控，查看运行状态和日志。
- `设置`：配置 X Cookies、轮询间隔、电报 Bot、飞书 Webhook、币安广场 API Key、重试参数和本地状态文件。

## 基础配置

首次运行建议在 `设置` 页面完成配置：

- `X Cookies 文件`：默认 `.twikit_cookies.json`，用于真实抓取 X。
- `轮询间隔（秒）`：默认 `300`。
- `电报 Bot Token` 和 `电报 Chat ID`：用于发送电报通知。
- `飞书 Webhook URL` 和可选 `飞书签名密钥`：用于通过飞书自定义机器人推送通知。
- `币安广场 API Key`：用于发布到币安广场。
- `币安广场发布`：只发送清理后的完整纯正文，不追加原推链接，也会移除正文中的 URL；如果正文超过币安广场接口限制，会记录失败原因，不再静默截断成 `...`。

## 环境变量示例

`env.example` 提供了可复制的默认配置。核心目标配置如下：

```json
[
  {
    "username": "elonmusk",
    "enabled": true,
    "include_replies": false,
    "platforms": {
      "telegram": {
        "enabled": true,
        "include_keywords": "",
        "exclude_keywords": ""
      },
      "feishu": {
        "enabled": false,
        "include_keywords": "",
        "exclude_keywords": ""
      },
      "binance_square": {
        "enabled": false,
        "include_keywords": "btc",
        "exclude_keywords": "spam"
      }
    }
  }
]
```

说明：

- `telegram.enabled=true` 表示该目标会推送到电报。
- `feishu.enabled=true` 表示该目标会推送到飞书 Webhook。
- `binance_square.enabled=true` 表示该目标会发布到币安广场。
- `include_replies=false` 表示只监控主贴；改为 `true` 后会同时监控该用户的回复。回复默认只进入电报和飞书通知，币安广场会跳过。
- 未设置关键词时，会匹配该目标抓到的全部新推文。
- 币安广场没有单独的全局开关需要配置，是否发布由每个目标的币安广场开关决定。

## 命令行模式

Web UI 是推荐入口；如果只想跑命令行，可以使用：

```powershell
python -m src.main --once
python -m src.main
```

`--once` 只执行一轮，适合验证配置；不带参数会按轮询间隔持续运行。

## 文件说明

- `src/collector/`：X 采集与抓取错误简化。
- `src/notifier/telegram_notifier.py`：电报推送模块。
- `src/notifier/feishu_notifier.py`：飞书 Webhook 推送模块。
- `src/notifier/binance_square_notifier.py`：币安广场发布模块。
- `src/runtime/`：运行循环、目标状态、平台推送状态汇总。
- `src/store/dedup_store.py`：采集去重。
- `src/store/delivery_status_store.py`：平台推送状态持久化。
- `src/web/static/index.html`：本地 Web 控制台。

## 开发文档

- [ChirpScan 项目总设计](docs/architecture.md)
- [ChirpScan 3.0 开发文档：荣辱柱系统与列表价值抓取](docs/ChirpScan-3.0-dev.md)
