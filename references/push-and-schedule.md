# 推送与定时任务

## 一、配置推送渠道

1. 复制 `config.example.json` 到 `~/.anhui-legal-radar/config.json`（Windows：`%USERPROFILE%\.anhui-legal-radar\config.json`）。
2. 把要用的渠道改为 `"enabled": true`。
3. 把密钥写进**环境变量**，配置文件里只写变量名（`*_env` 字段）。不要把密钥提交到 Git，也不要发给 AI。
4. 测试：`python scripts/radar.py push --report <任一报告.md>`。

| 渠道 | `type` | 需要的环境变量 | 获取方式 |
|---|---|---|---|
| 邮箱 | `email` | `smtp_password_env` 指向 SMTP 授权码 | QQ 邮箱：设置 → 账户 → 开启 SMTP 服务，生成授权码；端口 465 |
| 企业微信群机器人 | `wecom` | `webhook_env` | 群设置 → 群机器人 → 添加，复制 Webhook 地址 |
| 钉钉群机器人 | `dingtalk` | `webhook_env`，可选 `secret_env`（加签） | 群设置 → 机器人 → 自定义；安全设置选「加签」 |
| 飞书群机器人 | `feishu` | `webhook_env`，可选 `secret_env` | 群设置 → 群机器人 → 自定义机器人 |
| 微信（Server酱） | `serverchan` | `sendkey_env` | sct.ftqq.com 微信扫码登录获取 SendKey |
| 通用 Webhook | `webhook` | `url_env` | 收到 `{"title","markdown","report_name"}` 的 JSON POST |

设置环境变量：

```bash
# macOS / Linux：写入 ~/.zshrc 或 ~/.bashrc
export RADAR_SMTP_PASSWORD='你的授权码'
```

```powershell
# Windows：写入用户环境变量，新开终端后生效
[Environment]::SetEnvironmentVariable('RADAR_SMTP_PASSWORD', '你的授权码', 'User')
```

各平台的消息长度有限，推送内容是「新增 + 截止提醒」摘要；完整报告保存在本地，邮件会附上完整报告。

## 二、定时任务

### 推送逻辑（所有方式通用）

`radar.py daily` 每次运行都会：

1. 采集近 N 天的公告（默认 30 天，standard 深度）。
2. 和 `~/.anhui-legal-radar/state.json` 比对，找出**新增公告**（首次出现的可报名、待核实、意向、更正）和**截止提醒**（已推送过的可报名项目，在截止前 3 天和 1 天各提醒一次）。
3. 有新增或提醒才推送（可用 `push_when_empty` 或 `--always` 改为每天都推送）。
4. 至少一个渠道推送成功后才更新 state，失败的内容下次会再推送。

建议时间：工作日早上 8:00 左右，赶在上班前处理当天截止的项目。

### 方式 A：Agent 定时任务（模型核实，准确度高）

在下列平台新建定时任务，频率选「每天 08:00」，提示词填写：

```text
使用 anhui-legal-tender-radar skill 执行每日推送：
1. 运行 radar.py collect --days 30。
2. 按 SKILL.md 第 1.5 步做搜索引擎兜底：运行 radar.py web-queries，用你的联网搜索逐条执行，把具体公告的链接写进 leads.json，再运行 radar.py add-leads。
3. 按 SKILL.md 的口径逐条核实 open、review、change、intention 中的候选，写出 review.json。
4. 运行 radar.py daily --leads <leads.json> --review <review.json>，只推送新增公告和截止提醒。
5. 最后用三行汇报：新增几条、提醒几条、覆盖缺口。不要向我提问，遇到无法确定的内容放进「待核实」。
```

#### Codex

- 安装：把本仓库放到 `~/.codex/skills/anhui-legal-tender-radar/`（全局），或项目内 `.agents/skills/anhui-legal-tender-radar/`。
- 定时：在 Codex 应用中为该项目新建 Automation（自动化任务），设置每天运行，填入上面的提示词。自动化在 Codex 应用运行时触发，结果出现在收件箱中。
- 如果只用 Codex CLI，可以用系统定时任务调用 `codex exec "<上面的提示词>"`（需要事先在 Codex 中允许本目录的命令执行与联网）。

#### DeepSeek Harness（dsh）

- 安装：放到 `~/.dsh/skills/anhui-legal-tender-radar/`（用户级），或项目内 `.dsh/skills/`、`.agents/skills/`。dsh 按 `<目录名>/SKILL.md` 识别 skill，名称为 kebab-case。
- 定时：两种方式任选。
  - 官方实验功能：在插件管理页启用「自动化任务」（`@deepseek-ai/dsh-experimental-schedule-bundle`），然后直接对 Agent 说「每天早上 8 点运行一次：<上面的提示词>」，Agent 会调用 `schedule_create` 建立任务。注意：官方任务会把结果发回创建它的那个会话。
  - 社区插件：如 `dsh-cron-tasks`，每次在新会话中运行，有独立的运行历史，适合长期每日任务。
- dsh 的定时任务依赖 dsh 本体在运行，电脑关机或 dsh 退出时不会触发。

#### WorkBuddy

- 安装：放到 `~/.workbuddy/skills/anhui-legal-tender-radar/`，或通过 SkillHub 安装：`skillhub install anhui-legal-tender-radar --dir ~/.workbuddy/skills`（上架后可用）。
- 定时：在 WorkBuddy 的定时任务 / 自动化中新建每日任务，填入上面的提示词。

#### Claude Code 及其他 Agent

- Claude Code：`~/.claude/skills/anhui-legal-tender-radar/`。其他工具的目录见 SkillHub 安装文档。
- 没有内置定时任务的工具，用方式 B 的系统定时任务调用该工具的无交互命令。

### 方式 B：系统定时任务（不调用模型，零成本）

结果是规则初筛，报告和推送会标注「未经逐条核实」。适合放在服务器或常开电脑上。

**macOS / Linux（cron）**

```bash
crontab -e
# 每天 08:00（服务器时区须为北京时间；否则换算，或在命令前加 TZ=Asia/Shanghai）
0 8 * * * cd /path/to/anhui-legal-tender-radar && /usr/bin/python3 scripts/radar.py daily --quiet >> ~/.anhui-legal-radar/daily.log 2>&1
```

cron 不会读取 `~/.zshrc`，需要在 crontab 顶部写入环境变量，或改用 `bash -lc '...'` 执行。

**Windows（任务计划程序）**

```powershell
$action  = New-ScheduledTaskAction -Execute 'py' -Argument '-X utf8 scripts\radar.py daily --quiet' -WorkingDirectory "$env:USERPROFILE\skills\anhui-legal-tender-radar"
$trigger = New-ScheduledTaskTrigger -Daily -At 8:00
Register-ScheduledTask -TaskName 'AnhuiLegalRadar' -Action $action -Trigger $trigger -Description '安徽省法律服务招标信息每日推送'
```

任务以当前用户运行，可以读取用户环境变量。

### 先试运行

```bash
python scripts/radar.py daily --dry-run --print   # 不推送、不写状态，只打印推送内容
```

第一次正式运行时，近 30 天内所有符合条件的公告都会被当成「新增」推送一次，之后只推送变化。
