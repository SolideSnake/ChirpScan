# ChirpScan 项目总设计

## 1. 项目定位

ChirpScan 是一个本地运行的 X 监控与多平台推送工具。系统从 X 采集指定账号的新推文，经过去重和目标级平台规则过滤后，推送到电报、飞书和币安广场。

当前主线设计目标：

- 单机运行，配置简单，优先保证可控和可排查。
- X 采集与平台推送解耦，新增平台时不影响采集层。
- 每个 X 目标可以独立选择推送平台，并为每个平台配置关键词规则。
- 通知通道用于自用或团队内提醒，发布通道用于对外发布。
- 保留本地状态，避免重复处理和重复发布。

## 2. 当前能力边界

已实现能力：

- X 账号轮询采集，支持 `twikit` 真实抓取和 `mock` 本地测试。
- 统一目标配置 `MONITOR_TARGETS`。
- 目标级平台开关：电报、飞书、币安广场。
- 平台级关键词规则：包含和排除。
- 采集去重：避免同一条推文重复进入处理队列。
- 平台推送状态：币安广场发布状态持久化，避免重复发文。
- 本地 Web UI：目标管理、设置、运行控制、日志查看。
- 测试推送：用于验证各平台配置是否可用。

当前不做的事：

- 不自动上传图片到币安广场。
- 不做公开 Web 服务部署。
- 不做多用户权限系统。
- 不做自动交易。
- 不把 AI 荣辱柱功能纳入当前 2.x 主流程。

## 3. 系统架构

核心数据流：

```text
Settings
  ↓
RuntimeManager
  ↓
RuntimeContext
  ↓
TwitterCollector
  ↓
DedupStore
  ↓
InMemoryMessageQueue
  ↓
Publishers
  ├─ TelegramNotifier
  ├─ FeishuNotifier
  └─ BinanceSquareNotifier
  ↓
DeliveryStatusStore
  ↓
Web UI / API Status
```

模块职责：

- `src/config/`：读取环境变量和 UI 保存配置，构建 `Settings`、`MonitorTarget`、`PlatformRoute`。
- `src/collector/`：负责 X 推文抓取、抓取错误简化、采集结果转换为 `TweetEvent`。
- `src/filters/`：负责关键词表达式标准化和匹配。
- `src/models/`：定义跨模块传递的数据模型，例如 `TweetEvent`。
- `src/queue/`：当前使用内存队列连接采集和推送。
- `src/notifier/`：平台推送模块，电报、飞书、币安广场互相独立。
- `src/runtime/`：构建运行上下文，执行采集循环，汇总状态。
- `src/store/`：本地状态持久化，包括采集去重和平台推送状态。
- `src/web/`：FastAPI 接口和单页 Web UI。
- `src/honor_board/`：3.0 荣辱柱预留域服务，当前不参与主流程决策。

## 4. 核心模型

### TweetEvent

`TweetEvent` 是采集层传给推送层的统一事件模型：

```text
tweet_id
author
text
url
created_at
```

设计原则：

- 采集层尽量保留原始正文，不做平台专属裁剪。
- 平台特殊处理放在各自 Notifier 内完成。
- 未来如需支持媒体，应扩展模型，而不是让正文混入媒体上传状态。

### MonitorTarget

`MonitorTarget` 表示一个被监控的 X 用户：

```text
username
enabled
platforms
```

`platforms` 是按平台 ID 索引的 `PlatformRoute`：

```text
enabled
include_keywords
exclude_keywords
```

设计原则：

- 一个 X 用户只出现一次。
- 平台开关属于目标，不属于全局。
- 不同平台关键词互不影响。

### DeliveryRecord

`DeliveryRecord` 表示某个平台对某条推文的一次处理结果：

```text
platform
tweet_id
status
reason
external_id
attempts
url
payload_text
retryable
updated_at
```

当前持久化重点是币安广场，因为重复对外发文风险最高。电报和飞书默认作为通知通道，不做跨重启防重复状态。

## 5. 配置设计

配置来源：

- 环境变量。
- Web UI 保存到 `.state/ui_config.json` 后再加载到环境。

关键配置：

- `TWITTER_PROVIDER`：`twikit` 或 `mock`。
- `MONITOR_TARGETS`：统一目标配置。
- `TWIKIT_COOKIES_FILE`：X 登录 Cookies 文件。
- `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`：电报配置。
- `FEISHU_WEBHOOK_URL`、`FEISHU_SECRET`：飞书 Webhook 配置。
- `BINANCE_SQUARE_API_KEY`：币安广场配置。
- `DEDUP_FILE`、`DEDUP_MAX_IDS`：采集去重状态。
- `DELIVERY_STATUS_FILE`、`DELIVERY_STATUS_MAX_RECORDS`：平台推送状态。
- `RETRY_MAX_ATTEMPTS`、`RETRY_BASE_DELAY_SEC`：通知通道重试。
- `BINANCE_RETRY_MAX_ATTEMPTS`、`BINANCE_RETRY_BASE_DELAY_SEC`：币安广场重试。

兼容策略：

- 旧的 `ALERT_TARGETS` 和 `PUBLISH_TARGETS` 仍可读取并合并到 `MONITOR_TARGETS`。
- 旧的 `SYNC_STATUS_FILE` 仍可作为 `DELIVERY_STATUS_FILE` 的 fallback。
- 旧的 `BINANCE_PUBLISH_TEMPLATE` 保留兼容读取，但当前币安广场实际只发布清理后的纯正文。

## 6. 采集设计

采集器：

- `MockTweetSource`：本地测试源。
- `TwikitTweetSource`：真实 X 抓取源。
- `TwitterCollector`：统一调用 source、记录目标级抓取结果、执行去重。

Twikit 采集策略：

- 优先加载 Cookies。
- 请求 X 用户信息和用户推文列表。
- 对 twikit 请求签名异常做降级重试。
- 对常见异常输出更易读的中文错误。
- 单次失败先记为波动，连续失败达到阈值后才升级为错误。

去重策略：

- `DedupStore` 保存已处理过的 `tweet_id`。
- 同一条推文第二次采集不会再进入推送队列。
- 去重属于采集层，不属于任何单一平台。

## 7. 过滤与路由设计

过滤表达式：

- 多个规则用英文逗号分隔。
- 组合命中用 `+`，例如 `launchpool+binance`。
- 包含规则为空时，默认匹配全部推文。
- 排除规则为空时，默认不排除。

路由规则：

- 先检查目标是否启用。
- 再检查平台是否启用。
- 再执行该平台自己的关键词规则。
- 任一平台被过滤，不影响其他平台。

## 8. 推送平台设计

### 电报

定位：

- 给自己看的即时通知。

行为：

- 使用 Bot API `sendMessage`。
- 消息包含作者、正文、原推链接和时间。
- 禁用网页预览。
- 失败按通知重试配置重试。
- 不持久化防重复状态。

### 飞书

定位：

- 团队或工作流通知。

行为：

- 使用飞书自定义机器人 Webhook。
- 第一版只发送 `text` 消息。
- 支持可选签名密钥。
- 消息包含作者、正文、原推链接和时间。
- 失败按通知重试配置重试。
- 不持久化防重复状态。

### 币安广场

定位：

- 对外发布平台。

行为：

- 使用币安广场公开纯文本发文接口。
- 只发布清理后的正文。
- 不追加原推链接。
- 移除正文中的 URL，避免图片或卡片链接变成无效链接。
- 保留正文换行和空行。
- 清理后正文为空时跳过发布。
- 成功状态写入 `DeliveryStatusStore`，避免重复发文。

## 9. Runtime 设计

`RuntimeManager` 负责 Web UI 的运行控制：

- 启动、停止、重启监控任务。
- 加载和保存 UI 配置。
- 提供 `/api/status` 状态快照。
- 提供 `/api/logs` 和 `/api/logs/clear`。
- 汇总目标抓取状态和平台推送状态。

`run_cycle()` 是一轮核心处理：

1. 调用采集器获取新推文。
2. 把新推文放入内存队列。
3. 对每条推文找到对应目标。
4. 依次调用所有平台 Notifier。
5. 汇总 `PublishAttempt`。
6. 调用后置 hook，例如未来荣辱柱服务。

## 10. Web UI 设计

当前只有两个一级入口：

- `X 监控`
- `设置`

`X 监控` 页面：

- 运行状态栏。
- 启动、停止、重启、测试推送。
- 监控目标列表。
- 每个目标展示平台卡片：电报、飞书、币安广场。
- 每个平台可独立开关和配置关键词。
- 日志终端显示业务化日志。

`设置` 页面：

- X 采集设置。
- 电报设置。
- 飞书设置。
- 币安广场设置。
- 运行与存储设置。
- 保存设置。

前端设计原则：

- 目标编辑自动保存。
- 运行中的配置变更需要重启监控任务后生效。
- 平台列表由后端 `available_platforms` 驱动，减少新增平台时的硬编码。

## 11. 状态与日志

状态文件：

- `.state/dedup.json`：采集去重状态。
- `.state/delivery_status.json`：平台推送状态。
- `.state/ui_config.json`：Web UI 保存配置。

日志设计：

- 后端仍保留标准 logging 输出。
- Web UI 内存保存最近日志。
- 前端把常见技术日志简化为业务说明。
- ERROR / WARNING / INFO 支持前端筛选。

## 12. 扩展方式

新增推送平台时，推荐步骤：

1. 新增 `src/notifier/{platform}_notifier.py`。
2. 实现 `EventPublisher.process_event()`。
3. 在 `src/notifier/registry.py` 注册平台。
4. 在 `build_publishers()` 加入实例。
5. 在 `Settings` 和 `RuntimeManager` 中加入配置映射。
6. 在设置页增加平台配置字段。
7. 补充平台单元测试和运行管线测试。

是否持久化平台状态的判断：

- 对外发布平台：建议持久化，避免重复公开发布。
- 自用通知平台：默认不持久化，依赖采集去重即可。
- 如果通知平台未来需要失败补发，再接入 `DeliveryStatusStore`。

## 13. 测试策略

当前测试重点：

- 运行管线能按目标和平台开关执行。
- 电报、飞书、币安广场互不影响。
- 币安广场正文清理符合预期。
- 飞书 Webhook payload 和签名符合预期。
- 配置加载能构建平台路由。

推荐每次提交前执行：

```powershell
python -m unittest discover -s tests
node -e "const fs=require('fs'); const html=fs.readFileSync('src/web/static/index.html','utf8'); const scripts=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m=>m[1]); for (const script of scripts) new Function(script); console.log('JS_OK');"
```

## 14. 与 3.0 规划的关系

本文档描述当前项目总架构和 2.x 主线能力。

3.0 的荣辱柱系统、AI 观点提炼、AI 自动复核和列表价值抓取属于未来扩展规划，详见：

- `docs/ChirpScan-3.0-dev.md`

3.0 应优先复用当前架构中的采集、过滤、通知、状态和 Web UI 能力，而不是另起一套系统。

## 15. 回复监控策略

每个 `MonitorTarget` 可以配置 `include_replies`：

- `false`：默认策略，只抓取目标用户主页主贴，保持 2.0 原有行为。
- `true`：抓取目标用户的主贴和回复，回复与主贴共用同一套平台开关和关键词规则。

采集层会把回复解析为 `TweetEvent(tweet_type="reply")`，并保留 `in_reply_to_status_id`、`in_reply_to_user` 和 `conversation_id`。电报和飞书会把回复显示为“回复了 @xxx”，适合做预警通知；币安广场默认跳过回复，避免把互动回复当作对外内容发布。
