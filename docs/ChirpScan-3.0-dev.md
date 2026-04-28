# ChirpScan 3.0 开发文档：荣辱柱系统与列表价值抓取

## 背景

ChirpScan 2.0 已经具备 X 监控、电报推送、币安广场发布、关键词过滤、去重和平台推送状态记录。3.0 的目标不是重写 2.0，而是在现有采集和推送能力之上增加两个新能力：

- `荣辱柱系统`：用户把一条 X 推文链接发给电报机器人，系统抓取并保存推文原文，由 AI 提炼核心观点和预测，再随着时间推移追踪这些观点是否兑现。
- `列表价值抓取`：用户在软件里导入一批 X 用户或 X List，系统每天抓取这些账号的推文，筛选有价值内容后推送到电报。

## 产品目标

- 把“值得以后回头验证的推文”沉淀为结构化观点卡片。
- 让 AI 帮忙提炼观点，但不让 AI 无证据地下结论。
- 把预测类观点变成可跟踪对象，支持状态、证据、时间线和人工复核。
- 从一批账号或列表中自动挑出高价值推文，降低信息噪音。
- 保持 2.0 的 X 监控、电报、币安广场推送逻辑稳定不变。

## 非目标

- 3.0 不自动公开羞辱或公开发布结论，默认只在本地 UI 和私有电报里展示。
- 3.0 不承诺 AI 判断 100% 正确，所有兑现/未兑现结论都必须保留证据和置信度。
- 3.0 不把币安广场作为荣辱柱输出渠道，币安广场仍只负责正常推文同步发布。
- 3.0 不在第一阶段做复杂社交关系分析、情绪交易信号或自动交易。

## 一级页面

3.0 推荐保留现有两个入口，并新增一个入口：

- `X 监控`：保持 2.0 的目标监控、平台推送和日志能力。
- `设置`：保持 2.0 的 X、电报、币安广场、存储和重试配置。
- `荣辱柱`：新增页面，用于查看观点卡片、验证状态、证据链和列表价值抓取结果。

后续如果列表抓取内容变多，可以再拆出 `情报流` 页面；第一版建议先放在 `荣辱柱` 页内的第二个 Tab，避免导航过早膨胀。

## 核心流程一：电报投稿到荣辱柱

### 用户流程

1. 用户在电报里把 X 推文链接发送给 ChirpScan Bot。
2. Bot 解析链接，提取 `tweet_id`、作者和 URL。
3. 系统通过现有 X 采集能力抓取该推文原文、发布时间、作者、媒体链接和引用关系。
4. 系统保存原始推文快照，避免后续原推删除或修改后丢失证据。
5. AI 提炼该推文的核心观点，识别其中是否包含预测、判断、承诺或可验证结论。
6. 前端 `荣辱柱` 页面展示观点卡片，默认状态为 `待跟踪` 或 `仅归档`。
7. 到达检查时间后，系统自动收集证据并让 AI 给出初步判断。
8. 用户可人工确认为 `已兑现`、`未兑现`、`部分兑现`、`证据不足` 或 `争议`。

### 推文链接格式

需要兼容：

- `https://x.com/{username}/status/{tweet_id}`
- `https://twitter.com/{username}/status/{tweet_id}`
- 带查询参数的链接，例如 `?s=20`
- 电报消息中混合文字和链接的情况

### AI 提炼输出

AI 输出必须是结构化 JSON，不能只返回自然语言。建议字段：

```json
{
  "summary": "一句话核心观点",
  "claim_type": "prediction",
  "claims": [
    {
      "text": "具体可验证观点",
      "subject": "BTC",
      "direction": "up",
      "metric": "price",
      "target": "100000",
      "deadline": "2026-12-31",
      "check_method": "检查 BTC 是否在截止日前达到 100000 USDT",
      "confidence": 0.74
    }
  ],
  "is_trackable": true,
  "risk_notes": "该观点缺少明确时间，可按默认 30 天观察"
}
```

### 观点类型

- `prediction`：可验证预测，例如价格、事件、上线、政策、财报、项目进展。
- `opinion`：主观看法，只归档，不自动判定兑现。
- `claim`：事实性陈述，可验证真假。
- `promise`：作者承诺自己会做某件事。
- `signal`：交易或市场信号，适合归档和复盘，但不一定自动判定。

### 状态机

```text
submitted -> archived -> tracking -> needs_review -> resolved
```

业务状态建议：

- `待处理`：刚收到链接，还没完成抓取或 AI 提炼。
- `仅归档`：没有可验证预测，只保存观点。
- `跟踪中`：已识别可验证观点，等待检查时间。
- `待复核`：AI 已给出初步判断，需要用户确认。
- `已兑现`：有证据支持观点实现。
- `未兑现`：有证据支持观点失败。
- `部分兑现`：部分条件满足。
- `证据不足`：无法可靠判断。
- `争议`：AI 或用户认为结论存在解释空间。

## 核心流程二：自动判断预测是否实现

### 检查触发

- 每天定时扫描 `跟踪中` 的观点。
- 如果观点有明确截止时间，则在截止时间后检查。
- 如果没有明确截止时间，默认观察周期可以设为 7 天、30 天、90 天，由设置页配置。
- 用户也可以在前端手动点击 `立即复核`。

### 证据来源

第一阶段只做低风险证据：

- 原作者后续推文。
- 用户配置的 X 账号或列表中相关关键词推文。
- 推文原始链接和引用链。
- 手动粘贴的补充证据链接或文字。

第二阶段再考虑接入外部数据：

- 行情价格数据。
- 项目公告、交易所公告、新闻源。
- GitHub Release 或官方博客。

### AI 判断原则

- AI 只能给出 `建议状态`，不能直接永久定案。
- 每次判断必须输出证据摘要、引用来源、置信度和不确定点。
- 低置信度结论自动进入 `待复核`，不能直接标为已兑现或未兑现。
- 如果证据互相冲突，状态应为 `争议` 或 `证据不足`。

### 自动判断输出

```json
{
  "suggested_status": "fulfilled",
  "confidence": 0.82,
  "evidence": [
    {
      "type": "tweet",
      "url": "https://x.com/example/status/123",
      "summary": "作者确认目标已完成"
    }
  ],
  "reasoning": "原观点预测某功能会在 4 月上线，证据显示官方在 4 月 20 日宣布上线。",
  "needs_human_review": true
}
```

## 核心流程三：列表价值推文抓取

### 用户流程

1. 用户在软件里新增一个 `价值列表`。
2. 列表来源可以是手动用户名列表，也可以是 X List URL。
3. 系统每天抓取列表内账号的推文。
4. 先用硬规则过滤垃圾内容，例如转推、抽奖、无意义短句、纯链接。
5. 再用关键词和 AI 评分筛选高价值内容。
6. 达到阈值的内容推送到电报。
7. 前端展示每日入选推文、评分、原因和是否已推送。

### 列表来源

- `manual_users`：用户直接粘贴用户名列表。
- `x_list_url`：用户粘贴 X List 链接。
- `x_list_id`：用户提供已有 List ID。

第一阶段建议优先做 `manual_users`，因为稳定、可控、实现成本最低。X List URL 解析和成员同步作为第二阶段。

### 价值评分维度

建议总分 0-100：

- `信息密度`：是否包含新信息、数据、观点或事实。
- `可行动性`：是否有明确事件、时间、项目、风险或机会。
- `可信度`：作者是否可靠，是否有来源或证据。
- `稀缺性`：是否不是重复新闻或低质量转述。
- `相关性`：是否匹配用户关注的币种、项目、叙事或关键词。

### AI 筛选输出

```json
{
  "score": 86,
  "category": "market_signal",
  "reason": "包含明确项目进展和时间点，且与用户关注关键词匹配。",
  "should_notify": true,
  "summary": "某项目将在本周发布主网升级计划。"
}
```

### 电报推送格式

```text
高价值推文

作者：@example
评分：86
类别：market_signal

摘要：某项目将在本周发布主网升级计划。

原文：...
链接：https://x.com/example/status/123
```

## 推荐架构

### 新增模块

```text
src/
  telegram_bot/
    bot.py                 # 电报机器人收件入口
    parser.py              # 电报消息和 X 链接解析
  honor_board/
    models.py              # 荣辱柱数据模型
    repository.py          # SQLite 持久化
    service.py             # 观点归档、提炼、复核流程
    ai_extractor.py        # AI 观点提炼
    ai_judge.py            # AI 兑现判断
    scheduler.py           # 定时复核
  list_watch/
    models.py              # 价值列表和抓取结果模型
    source.py              # 手动列表 / X List 来源
    scorer.py              # 规则 + AI 评分
    service.py             # 每日抓取和推送流程
```

### 复用现有模块

- `src/collector/`：复用 X 抓取能力，必要时增加 `fetch_by_tweet_url` 和 `fetch_list_timeline`。
- `src/filters/`：复用关键词表达式和硬过滤逻辑。
- `src/notifier/telegram_notifier.py`：复用电报发送能力。
- `src/runtime/`：增加荣辱柱定时任务和列表抓取任务。
- `src/web/static/index.html`：新增 `荣辱柱` 页面。

### 存储建议

3.0 建议引入 SQLite，而不是继续把荣辱柱写成 JSON：

- 荣辱柱需要按状态、作者、时间、标签、置信度查询。
- 观点会不断追加证据和判断历史。
- 列表抓取每天会产生大量候选推文。
- SQLite 本地部署简单，适合当前单机应用。

推荐文件：

```text
.state/chirpscan.db
```

## 数据模型草案

### archived_tweets

| 字段 | 说明 |
| --- | --- |
| id | 内部 ID |
| tweet_id | X 推文 ID |
| author | 作者用户名 |
| url | 原始链接 |
| text | 原文 |
| created_at | 推文发布时间 |
| archived_at | 归档时间 |
| media_json | 图片、视频、卡片等媒体信息 |
| raw_json | 原始抓取响应快照 |

### honor_claims

| 字段 | 说明 |
| --- | --- |
| id | 内部 ID |
| tweet_id | 关联 archived_tweets |
| summary | 核心观点摘要 |
| claim_text | 可验证观点 |
| claim_type | prediction / opinion / claim / promise / signal |
| status | 跟踪状态 |
| subject | 标的或主题 |
| deadline | 截止时间 |
| check_method | 检查方式 |
| confidence | AI 提炼置信度 |
| created_at | 创建时间 |
| updated_at | 更新时间 |

### honor_evidence

| 字段 | 说明 |
| --- | --- |
| id | 内部 ID |
| claim_id | 关联观点 |
| evidence_type | tweet / url / manual / market_data |
| source_url | 证据链接 |
| summary | 证据摘要 |
| raw_text | 证据原文 |
| collected_at | 收集时间 |

### honor_judgements

| 字段 | 说明 |
| --- | --- |
| id | 内部 ID |
| claim_id | 关联观点 |
| suggested_status | AI 建议状态 |
| final_status | 用户确认后的状态 |
| confidence | 判断置信度 |
| reasoning | 判断理由 |
| created_by | ai / user |
| created_at | 创建时间 |

### watch_lists

| 字段 | 说明 |
| --- | --- |
| id | 内部 ID |
| name | 列表名称 |
| source_type | manual_users / x_list_url / x_list_id |
| source_value | 用户名列表、URL 或 List ID |
| enabled | 是否启用 |
| schedule | 抓取频率 |
| min_score | 电报推送最低分 |
| include_keywords | 包含关键词 |
| exclude_keywords | 排除关键词 |

### valuable_tweets

| 字段 | 说明 |
| --- | --- |
| id | 内部 ID |
| watch_list_id | 关联列表 |
| tweet_id | 推文 ID |
| author | 作者 |
| text | 原文 |
| score | 价值分 |
| category | 分类 |
| reason | 入选理由 |
| notified_at | 推送时间 |
| created_at | 入库时间 |

## API 草案

### 荣辱柱

- `POST /api/honor/import-url`：导入一条 X 推文链接。
- `GET /api/honor/claims`：查询观点卡片，支持状态和作者筛选。
- `GET /api/honor/claims/{id}`：查看观点详情、原文、证据和判断历史。
- `POST /api/honor/claims/{id}/review`：手动触发 AI 复核。
- `PATCH /api/honor/claims/{id}`：人工修改状态、标签、备注。
- `POST /api/honor/evidence`：手动添加证据。

### 电报 Bot

- `POST /api/telegram/webhook`：接收电报 Webhook。
- `POST /api/telegram/poll-once`：开发环境手动拉取一次 Bot 消息。

第一版为了简单，可以不用 Webhook，直接由后台任务轮询 Bot Updates。

### 价值列表

- `GET /api/watch-lists`：查询列表。
- `POST /api/watch-lists`：新增列表。
- `PATCH /api/watch-lists/{id}`：更新列表配置。
- `DELETE /api/watch-lists/{id}`：删除列表。
- `POST /api/watch-lists/{id}/run`：手动执行一次抓取和评分。
- `GET /api/valuable-tweets`：查询已入选推文。

## 前端 UI 草案

### 荣辱柱页面布局

- 顶部状态栏：待复核、跟踪中、已兑现、未兑现、证据不足数量。
- Tab 1：`观点卡片`
- Tab 2：`投稿记录`
- Tab 3：`价值列表`
- Tab 4：`高价值推文`

### 观点卡片字段

- 作者和原推链接。
- 原文折叠展示。
- AI 提炼的一句话观点。
- 状态徽标：跟踪中、已兑现、未兑现、部分兑现、证据不足、争议。
- 截止时间和检查方式。
- AI 置信度。
- 证据数量。
- 操作：查看详情、立即复核、人工定案、添加证据。

### 价值列表页面

- 左侧：列表配置，包括列表名、来源、用户名列表、关键词、最低评分、是否启用。
- 右侧：最近抓取结果，包括推文、评分、入选理由、是否已推送。
- 顶部操作：新增列表、手动运行、保存。

## 配置项草案

```text
HONOR_BOARD_ENABLED=true
HONOR_BOARD_DB_FILE=.state/chirpscan.db
HONOR_BOARD_DEFAULT_REVIEW_DAYS=30

TELEGRAM_BOT_INBOX_ENABLED=true
TELEGRAM_BOT_POLL_INTERVAL_SEC=10
TELEGRAM_BOT_ALLOWED_USER_IDS=

AI_PROVIDER=openai
AI_MODEL=
AI_API_KEY=

WATCH_LISTS_ENABLED=true
WATCH_LIST_DAILY_RUN_AT=09:00
WATCH_LIST_DEFAULT_MIN_SCORE=75
WATCH_LIST_MAX_TWEETS_PER_DAY=200
```

安全建议：

- `TELEGRAM_BOT_ALLOWED_USER_IDS` 必须支持白名单，避免陌生人向荣辱柱投稿。
- AI Key 不进入前端，不写入日志。
- 原始推文和 AI 判断都存在本地，默认不外发到公开平台。

## 任务拆分

### Phase 1：荣辱柱最小闭环

- 新增 SQLite 存储和 repository。
- 新增 X 推文链接解析。
- 新增 `POST /api/honor/import-url`。
- 复用采集器抓取单条推文原文。
- 前端新增 `荣辱柱` 页面和观点列表。
- 暂时不接 AI，先人工填写摘要或用占位摘要。

验收标准：

- 用户粘贴一条 X 链接后，系统能保存原文并在前端看到。
- 重复投稿同一条推文不会重复创建归档。
- 页面能按状态筛选观点。

### Phase 2：电报 Bot 投稿

- 新增电报 Bot 收件轮询。
- 解析电报消息中的 X 链接。
- 限制允许投稿的 Telegram 用户 ID。
- 投稿成功后 Bot 回复“已归档”。

验收标准：

- 用户把 X 链接发给 Bot，前端能看到对应归档。
- 非白名单用户投稿会被拒绝。
- 无效链接会返回清楚错误。

### Phase 3：AI 提炼观点

- 新增 AI adapter。
- 新增 `ai_extractor.py`，输出结构化观点。
- 支持人工编辑 AI 生成的观点。
- 给无法验证的内容标记为 `仅归档`。

验收标准：

- 每条投稿能生成核心观点摘要。
- 预测类内容能生成 deadline、check_method 和 confidence。
- AI 输出 JSON 解析失败时不会影响主流程。

### Phase 4：AI 自动复核

- 新增复核调度器。
- 自动收集证据。
- 新增 `ai_judge.py`。
- 前端展示证据链和 AI 建议状态。

验收标准：

- 到期观点能进入 `待复核`。
- AI 判断必须带证据和置信度。
- 用户能人工确认最终状态。

### Phase 5：列表价值抓取

- 新增 `list_watch` 模块。
- 支持手动用户名列表。
- 每天抓取列表内账号推文。
- 规则过滤 + AI 评分。
- 入选内容推送到电报。

验收标准：

- 用户能创建列表并手动运行。
- 低分内容不会推送。
- 高分内容能推送到电报，并在前端显示评分和理由。

### Phase 6：X List 来源

- 支持 X List URL / List ID。
- 同步 List 成员。
- 成员变化可在前端展示。

验收标准：

- 用户粘贴 X List URL 后能解析并抓取列表成员推文。
- List 成员失败时不影响手动列表。

## 风险与处理

- X 接口不稳定：沿用 2.0 的抓取降级、连续失败告警和重试机制。
- AI 误判：所有结论保留证据、置信度和人工确认入口。
- 数据膨胀：列表抓取候选推文需要设置每日上限和定期清理策略。
- 隐私风险：电报投稿白名单必须默认开启，敏感 Key 不进入日志。
- UI 复杂：第一版只做观点卡片和列表结果，不急着做复杂大屏。

## 推荐优先级

最推荐先做：

1. SQLite 存储。
2. X 链接归档。
3. 荣辱柱前端页面。
4. 电报 Bot 投稿。
5. AI 提炼核心观点。
6. 手动用户名列表价值抓取。

暂缓：

- 自动最终定案。
- 外部行情和新闻数据。
- X List 成员自动同步。
- 公开展示或公开发布荣辱柱结论。

## 结论

ChirpScan 3.0 应该把“推文同步工具”升级成“观点归档与验证工具”。荣辱柱系统负责长期记忆和复盘，列表价值抓取负责日常信息筛选。两者都复用现有 X 采集、电报推送、关键词过滤和状态管理能力，但新增 SQLite、AI 提炼、AI 复核和前端荣辱柱页面。
