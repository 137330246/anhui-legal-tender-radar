# 安徽省法律服务招标信息收集和推送skill

> **预览版 v0.9.2**：功能完整，但规则只经过少量真实数据检验，可能有误收或漏检。欢迎试用并反馈（见文末「反馈」）。已知问题见 [CHANGELOG.md](CHANGELOG.md)。官方接口最后验证可用：2026-11-15。

一个给律师和律所用的 AI Agent Skill：自动检索**安徽省**的法律服务招标、采购、比选、入库和破产管理人选任公告，逐条读正文核实后出报告，并可以每天定时推送到邮箱、企业微信、钉钉、飞书或微信。

可以在 Claude Code、Codex、DeepSeek Harness（dsh）、WorkBuddy 等支持 `SKILL.md` 的 Agent 中使用，也可以不用 AI，直接用 Python 脚本定时运行。

## 能做什么

- **覆盖 6 类官方来源**：安徽省政府采购网、安徽省公共资源交易平台、合肥市公共资源交易中心、安徽省招标投标信息网（国企、金融机构、医院等非政府采购）、全国企业破产重整案件信息网，以及 125 家安徽各级法院官网。
- **搜索引擎兜底**：补官方接口覆盖不到的国企自有平台、银行和担保公司、央企在皖分公司、医院高校官网。可以用 Agent 自带的联网搜索，也可以配置 SearXNG、博查或 Tavily 让脚本自己搜。搜到的只当线索，一律回原文核实，与官方来源重复的以官方为准。
- **只保留律师能投的标**：排除中标结果、合同公告、法律服务平台建设、文书送达、审计评估、破产案件投资人招募、新闻报道等。
- **分组输出**：可报名（截止时间明确且未截止）、待核实、提前关注（采购意向）、更正 / 延期 / 终止。7 日内截止的项目单独标出。
- **如实标注覆盖情况**：某个来源失败或没翻完，就写「未完整检查」，不会说「没有公告」。
- **每日增量推送**：只推送新增公告，以及截止前 3 天、1 天的提醒；没有变化时不打扰。

## 快速开始

需要 Python 3.9 及以上，不需要安装任何第三方依赖。用启动脚本运行，它会自动寻找可用的 Python：

```bash
git clone https://github.com/137330246/anhui-legal-tender-radar.git
cd anhui-legal-tender-radar
sh scripts/radar.sh check                            # macOS / Linux；Windows 用 scripts\radar.cmd check
sh scripts/radar.sh collect --days 30 --depth quick
sh scripts/radar.sh report --run <上一步输出的 run 文件>
```

报告和数据默认保存在 `~/.anhui-legal-radar/`。

### 没有 Python 怎么办

先运行 `scripts\radar.cmd --version`（Windows）或 `sh scripts/radar.sh --version`（macOS / Linux）。输出版本号就说明可以用；提示 `PYTHON_NOT_FOUND` 就需要安装：

| 系统 | 安装方法 |
|---|---|
| Windows | 在 PowerShell 运行 `winget install Python.Python.3.12`；或从 [python.org](https://www.python.org/downloads/) 下载，安装时勾选「Add python.exe to PATH」 |
| macOS | 在终端运行 `xcode-select --install`；或从 [python.org](https://www.python.org/downloads/) 下载 |
| Linux | `sudo apt install python3` 或 `sudo dnf install python3` |

- 装好后重新打开终端或 Agent 即可。Python 已装在别处、但不在 PATH 中的，设置环境变量 `RADAR_PYTHON` 为它的完整路径。
- **WorkBuddy（Windows 版）自带 Python 3.13**，启动脚本会自动找到，不用另装。
- **实在不想装**：Agent 会改用「简易模式」，只靠自带的联网搜索和网页读取，不调用官方检索接口、不扫描法院官网，遗漏会明显增多，也不能自动推送。详见 [references/no-python.md](references/no-python.md)。

## 安装为 Skill

把整个目录放到对应 Agent 的 skills 目录下，目录名为 `anhui-legal-tender-radar`：

| Agent | 目录 |
|---|---|
| Claude Code | `~/.claude/skills/` |
| Codex | `~/.codex/skills/` 或项目内 `.agents/skills/` |
| DeepSeek Harness | `~/.dsh/skills/` 或项目内 `.dsh/skills/` |
| WorkBuddy | `~/.workbuddy/skills/` |

也可以用 SkillHub 安装：`skillhub install anhui-legal-tender-radar --dir <上表中的目录>`。SkillHub 不允许上传 Windows 批处理文件，所以从 SkillHub 安装的版本**没有** `radar.cmd`（`radar.sh` 仍然有）。Windows 上 Agent 会按 SKILL.md 自己寻找 Python，使用上没有区别；要在命令行手动运行，请从 GitHub 获取完整版本，或直接用 `py scripts/radar.py`。

然后直接对 Agent 说：

> 查一下安徽近一个月律所能投的法律服务招标

> 安徽最近有没有破产管理人选任公告

> 每天早上 8 点把安徽新出的法律服务招标推送到我的钉钉群

## 首次使用

第一次使用时，Agent 会先问你几个问题（每题都有默认选项，回复「默认」也行），然后帮你配好：

- 电脑上没有 Python 时：现在安装，还是先用简易模式？
- 新公告要不要推送？推到钉钉群、企业微信群、飞书群、邮箱还是微信？
- 要不要每天自动检索？几点？由 Agent 定时运行，还是由电脑系统定时运行？

选了推送，Agent 会告诉你去哪里复制机器人地址，录入后发一条测试消息确认。选了系统定时，会直接帮你装好定时任务。之后想改，跟 Agent 说「改一下推送」「不要定时了」即可。

## 定时推送

两种方式，见 [references/push-and-schedule.md](references/push-and-schedule.md)：

| 方式 | 适用 | 是否消耗模型额度 | 准确度 |
|---|---|---|---|
| Agent 定时任务（Codex 自动化 / dsh 定时任务 / WorkBuddy 定时任务） | 希望每条都经 AI 读正文核实 | 是 | 高 |
| 系统定时任务（cron / Windows 任务计划程序）运行 `radar daily` | 服务器、常开电脑，零成本 | 否 | 规则初筛，会标注「未经核实」 |

推送渠道：邮箱（SMTP）、企业微信群机器人、钉钉群机器人、飞书群机器人、Server酱（微信）、通用 Webhook。密钥只保存在本机（`secrets.json` 或环境变量），配置文件里只记名称。系统定时任务可用 `radar schedule install --time 08:00` 一键安装。

## 命令一览

| 命令 | 作用 |
|---|---|
| `check [--courts]` | 自检各接口 |
| `collect --days 30 [--depth quick\|standard\|full]` | 检索、初筛、读正文，输出 run 文件 |
| `list --run <run>` / `show --run <run> <id>` | 查看候选及其正文 |
| `read <url>` | 读取单条公告正文 |
| `report --run <run> [--review <review.json>]` | 生成 Markdown 报告 |
| `push --report <report.md>` | 按配置推送 |
| `web-queries [--depth full] [--cities 合肥,芜湖]` | 输出搜索兜底的查询词（供 Agent 用自带搜索执行） |
| `add-leads --run <run> --leads <leads.json>` | 把搜到的线索交回脚本：读原文、套规则、去重后并入 |
| `daily [--leads <leads.json>] [--review <review.json>] [--dry-run --print]` | 定时任务一条龙：只推送新增与截止提醒 |
| `status` | 查看配置状态：是否完成首次引导、推送渠道与密钥是否就绪、系统定时任务 |
| `onboard done / reset` | 记录或重置首次引导的选择 |
| `channel add / list / remove / test` | 管理推送渠道并发送测试消息 |
| `secret set / list / delete` | 在本机录入推送或搜索的密钥（不显示内容） |
| `schedule install / remove / status [--time 08:00] [--weekdays]` | 安装或删除系统定时任务（Windows 任务计划程序 / cron） |

## 局限

- 只读公开网页正文，不登录、不过验证码、不读 PDF / Word 附件。部分法院公告正文只在 PDF 中，会放入「待核实」。
- 官方网站随时可能改版，导致某个来源暂时失效；`check` 命令可以快速发现。
- 安徽省政府采购网和安徽法院网站群会封禁频繁访问的 IP。脚本已限速，建议每天只完整运行一次，不要在同一天里反复全量运行。
- 脚本的规则初筛会有误判，建议用 Agent 模式核实，或者自己点开原文确认。
- 不保证全网无遗漏。报名资格、截止时间以原公告为准。本工具不构成投标建议。

## 使用须知

- **仅供个人或本所检索使用**。请每天最多完整运行一次，不要高频轮询、批量部署或拿来做数据转售。这些官方网站是公共资源，访问过于频繁会被封禁，也会影响其他人使用。
- 脚本只访问公开网页，不登录、不绕过验证码或访问限制。被拒绝访问时如实记为覆盖缺口。
- 报告中的项目信息来自公开公告，使用时请以原公告为准。

## 目录结构

```
SKILL.md                    Agent 读取的流程与口径
config.example.json         推送与定时配置模板
references/                 来源说明、核实格式、推送与定时任务文档
scripts/radar.cmd           启动脚本（Windows），自动寻找 Python
scripts/radar.sh            启动脚本（macOS / Linux），自动寻找 Python
scripts/radar.py            命令行入口
scripts/sources.py          5 个官方检索接口与正文读取
scripts/courts.py           125 家安徽法院官网扫描
scripts/extract.py          公告类型、法律服务、安徽关联、截止时间等规则
scripts/websearch.py        搜索引擎兜底：查询分层与 SearXNG / 博查 / Tavily 后端
scripts/notify.py           推送渠道
scripts/onboard.py          首次引导、推送渠道、密钥与系统定时任务
scripts/paths.py            数据目录与本机密钥
scripts/data/               法院站点清单、安徽地名
tests/                      离线单元测试
```

## 致谢

数据接口与核实口径来自作者开发的「法律服务雷达」网站项目。

## 反馈

遇到以下情况请提交 Issue（附上报告中的原文链接）：

- **误收**：报告里出现了不是律师服务、不在安徽或已经截止的公告。
- **漏检**：你知道的安徽法律服务招标没有出现在报告里（请注明在哪个网站发布）。
- **来源失效**：`sh scripts/radar.sh check` 报错。
- **新来源**：你知道稳定的安徽国企、银行、医院采购平台。

## 许可

[MIT](LICENSE) © 2026 何杨律师
