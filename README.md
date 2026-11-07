<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="pic/logo/rivu-logo-horizontal-dark.svg">
    <img src="pic/logo/rivu-logo-horizontal.svg" alt="Rivu" width="280">
  </picture>
</p>

# Rivu

基于固定源码证据、理解仓库上下文的只读 AI 代码变更审查 Agent。输入 GitHub PR 或本地 Git 的 Base / Head，即可获得变更总览、经过独立核验的问题及对应源码证据。

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat)](backend/pyproject.toml)
[![Node.js 21+](https://img.shields.io/badge/Node.js-21%2B-07866F?style=flat)](frontend/package.json)

[快速开始](#快速开始) · [本地 Git 审查](#本地-git-审查) · [配置说明](#配置与数据边界) · [ReviewBench 评测](eval/reviewbench/README.md)

## 项目特点

- 🔍 **两种审查入口**：审查 GitHub PR，或已注册本地仓库中的提交变更。
- 🧠 **按需理解上下文**：结合固定 Diff，调查调用者、相关测试源码和 Base / Head 行为差异。
- 🧾 **证据与独立核验**：Reviewer 提交候选，Verifier 检查支持与反证；证据不足的问题保持未决。
- ⏱️ **可控运行**：支持并发、资源上限、进度查看、取消与恢复，已保存的候选不因后续失败丢失。
- 📬 **GitHub 交付**：复制审查评论，或通过独立 Publisher 手动发布、授权本次任务完成后自动发布；发布默认关闭。

支持 Python、TypeScript / JavaScript、Java、Go、Rust 的静态解析。

> **只读边界**：Rivu 不执行被审查仓库的代码，不安装目标依赖，不运行目标测试、构建或脚本，不触发目标 CI，也不自动修复或写回补丁。源码会发送到你配置的模型服务。

## 快速开始

准备 Git、Python 3.12+、[uv](https://docs.astral.sh/uv/getting-started/installation/)、Node.js 21+ 和 npm，以及支持原生 Tool Calling 与结构化提交的模型服务。

### 1. 安装后端

```sh
git clone https://github.com/waangzh/Rivu.git
cd Rivu
cp .env.example backend/.env
cd backend
uv sync --extra test
```

编辑 `backend/.env`，填入 DeepSeek 模型配置：

```dotenv
RIVU_PROVIDER=deepseek
OPENAI_API_KEY=your-deepseek-api-key
OPENAI_BASE_URL=https://api.deepseek.com
RIVU_MODEL=deepseek-flash
```

使用其他 Provider 时，按[环境变量示例](.env.example)调整端点和模型；`RIVU_MODEL_CONTEXT_TOKENS` 应与实际模型能力匹配。

在系统设置中可对已保存的 OpenAI 兼容供应商查询和搜索模型；供应商需提供兼容的模型列表接口。列表只提供模型名称，非内置预设模型需要手动确认原生工具调用能力。

### 2. 初始化数据库并启动

首次使用时初始化新的空数据库，默认路径为 `backend/.rivu/reviews.db`。已有当前架构数据库可执行增量迁移。

```sh
uv run alembic upgrade head
uv run uvicorn app.main:app
```

访问[健康检查](http://127.0.0.1:8000/health)，应返回 `{"status":"ok"}`；[API 文档](http://127.0.0.1:8000/docs)可查看接口。

<details>
<summary>从旧审查架构迁移时的数据保护</summary>

旧架构数据库不能原地转换。先停止旧 Worker 与 Publisher，备份数据库、Checkpoint 和 Artifact，并对账状态为 `uncertain` 的发布，再为新架构选择空数据库；自定义路径通过 `RIVU_DB_PATH` 设置。旧数据不会自动转换或删除，不要用 `alembic stamp` 跳过迁移。

</details>

### 3. 启动前端

另开终端，在仓库根目录执行：

```sh
cd frontend
npm install
npm run dev
```

打开 Vite 输出的地址，默认是 `http://localhost:5173`。开发服务器将 `/api` 和 `/health` 代理到后端 `8000` 端口。

## 如何使用

1. 输入 GitHub PR 地址，选择 Quick / Standard / Deep、调查重点、文件范围和资源上限，开始审查。本地提交审查见下节。
2. 在 **Overview / Changes / Findings / Activity / Publish** 查看总览、固定 Diff、问题证据、进度、材料缺口、用量与发布预览。
3. 按需取消或重试。恢复复用已完成工作，重试仍沿用原预算与硬截止时间；总览可单独重试。中断的模型请求可能再次计费。
4. 复制评论交由人工处理；需要直接写入 GitHub 时，先完成[发布配置](#交付到-github)与授权。

`confirmed` 表示通过证据准入与独立核验，`refuted` 表示被反证，`insufficient` 表示必要证据不足。**`completed` 只表示计划内审查完成，不代表穷尽缺陷或允许合并**；部分范围、失败、取消和未知用量不会显示成完整成功或零成本。Rivu 不能替代项目测试、CI 与人工审批。

## 本地 Git 审查

在 `backend/.env` 注册服务器可访问的仓库，然后重启后端：

```dotenv
RIVU_LOCAL_REPOSITORIES={"project":"/absolute/path/to/project"}
```

Windows 路径使用正斜杠，例如 `{"project":"D:/Code/project"}`。在新建审查页选择 **本地 Git**、仓库、Base 与 Head；可直接开始，也可先点击 **固定并预览快照**，预览不调用审查模型。

只审查已提交内容，忽略暂存、未暂存与未跟踪文件。内容比较采用唯一 merge base，准备后保存独立固定输入，原仓库后续移动或修改不影响审查与恢复。

支持 SHA-1 对象格式、普通 `.git/` 目录的仓库，包括 shallow、partial clone 和 alternates；准备时可能从 remote 补取对象。暂不支持 linked worktree、bare 仓库或未提交改动快照。本地任务也会将源码发送给模型，不支持 PR 讨论、跨任务增量复用或 GitHub 发布。

## 配置与数据边界

完整配置见[环境变量示例](.env.example)，常用项如下：

| 用途 | 配置 |
| --- | --- |
| 模型服务 | `RIVU_MODEL_PROVIDERS`、`RIVU_DEFAULT_PROVIDER`，或单 Provider 的 `RIVU_PROVIDER`、`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`RIVU_MODEL` |
| GitHub 读取 | `GITHUB_TOKEN`：按仓库访问需求配置，不用作发布凭据 |
| 成本与容量 | `RIVU_REVIEW_MAX_INPUT_TOKENS`、`RIVU_REVIEW_MAX_OUTPUT_TOKENS`、`RIVU_MODEL_CONTEXT_TOKENS` |
| 并发与期限 | `RIVU_REVIEW_CONCURRENCY`、`RIVU_REVIEW_TIMEOUT_SECONDS` |
| 数据与保留 | `RIVU_DB_PATH`、`RIVU_ARTIFACT_DIR`、`RIVU_RETENTION_DAYS` |

- **成本**：整轮累计 Token 与模型调用上限默认不设门禁；按需显式设置。工具、单 Agent 调用和总期限仍有限制，未知用量保留预留，恢复不清零。
- **首轮与补查**：调查、补查和核验使用单 Agent 调用熔断（默认 48 次）与模型上下文容量；正常追加历史以复用前缀缓存，仅在容量、持续慢响应或重复调查等压力下整理上下文。总期限不随 PR 规模自动延长；首轮在期限的 60% 前收尾，首轮核验最晚到 70%，补查可到 80%，其新候选核验使用剩余期限。有限累计预算在首轮阶段为补查保留约 20%（小预算按可用容量缩减），补查继续共用原账本和硬截止时间。首轮及其核验结束后才补查具体缺口，无进展不反复补查；未覆盖与未决仍如实交付。
- **外部依赖疑点**：当前不提供依赖源码查询或外部搜索。需要仓库外依赖行为才能判断的疑点，交付具体版本、缺失前提和待确认问题，不因资料缺失确认缺陷，也不对明确的外部缺口反复执行仓库补查。历史报告已归档的依赖证据继续保留校验。
- **数据**：当前为可信本地单用户应用，SQLite 与 Artifact 不提供多租户安全隔离。使用前确认源码外发边界；LangSmith 默认关闭，不要将 `.env`、凭据或运行产物提交到仓库。
- **多 Provider**：设置页提供 OpenAI、DeepSeek、智谱 GLM 和 Kimi 官方标准 API 预设，默认使用 OpenAI 兼容接口，不预设套餐地址；也可添加自定义 OpenAI 兼容服务。配置服务端 API Key 环境变量后保存，再测试模型的原生工具调用。预设模型的上下文和输出上限受 Rivu 服务端资源上限约束，Kimi 官方建议 Thinking 工具调用使用较大的输出额度；可在服务端资源策略内调整。已有 Anthropic Messages 和 Google Gemini 配置仍可使用。API Key 不从前端提交，连接测试会产生模型用量。
- **官方接口资料**：[DeepSeek 思考模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode)、[智谱思考模式](https://docs.bigmodel.cn/cn/guide/capabilities/thinking-mode)、[Kimi 思考模式](https://platform.kimi.ai/docs/guide/use-thinking-models)。内置模型能力采用对应资料与 LangChain 模型元数据，用户修改后标记为手动配置。
- **配置生效**：Provider 注册表、默认模型和保留天数可在设置页修改；普通单 Provider 的端点与模型也可继续设置。已有任务冻结 Provider ID、类型、端点、模型能力与推理配置，恢复时按冻结身份解析。没有 Provider ID 的旧任务按原快照中的 Provider 与端点恢复。

### 交付到 GitHub

发布默认关闭，仅支持可信的本地单用户部署。Review Agent 永远只读，Publisher 使用独立 PAT，绝不回退使用 `GITHUB_TOKEN`。

1. 配置 `RIVU_GITHUB_PUBLISH_TOKEN`、数字仓库 ID allowlist、PAT 用户数字 ID，以及至少 32 字符的 `RIVU_PUBLICATION_API_TOKEN`；字段见[环境变量示例](.env.example)。
2. 在 GitHub 核对 PAT 的 **Pull requests: write** grant，设置 `RIVU_GITHUB_PUBLISH_PULL_REQUESTS_WRITE_CONFIRMED=true`，再启用 `RIVU_GITHUB_PUBLISH_ENABLED=true`。
3. 在 Publish 页输入操作口令、选择问题、预检并检查实际 Summary / Inline，最后明确确认。操作口令不是 GitHub PAT，只保留在页面内存。

也可在创建任务时勾选 **本次审查完成后自动发布到 GitHub PR**，输入操作口令授权。授权仅绑定本次任务和固定快照；完整且无未决项的结果可自动发布，零问题只更新摘要，不自动批准 PR。不完整或无法准确定位的结果转为人工确认，失败与取消不自动发布。

发布前重新校验 PR 状态、固定 Base / Head 与报告版本。发布状态独立于审查结论；遇到 `uncertain` 先对账，不盲目重发，也不承诺跨系统 exactly-once。

<details>
<summary>可选：Webhook 增量审查</summary>

配置签名密钥、仓库 allowlist 并启用 `RIVU_INCREMENTAL_REVIEW_WEBHOOK_ENABLED` 后，`POST /api/webhooks/github` 可处理仓库自身分支的 `pull_request.synchronize`，每个 PR 默认每小时最多自动创建 4 个任务。新 Head 会停止旧运行，历史问题必须取得本轮证据并重新核验；仅符合保守复用条件的已完成范围会复用。

Webhook 自动启动与本次任务的自动发布是两项独立功能，不因开启其中一项而获得另一项授权。

</details>

## 开发与验证

后端采用 **Python / FastAPI / LangChain / SQLite**，前端采用 **Vue 3 / TypeScript / Vite**。核心实现位于[审查模块](backend/app/review/)。

```text
固定快照 → 规划与上下文调查 → Reviewer 候选 → 独立 Verifier
         → 事实报告与变更总览 → 人工选择 / 授权 → 独立 Publisher
```

在仓库根目录执行（已完成依赖安装）：

```sh
cd backend
uv run pytest
uv run ruff check .
cd ../frontend
npm test
npm run build
cd ..
git diff --check
```

开发测试可执行 Rivu 自身代码，不代表产品可以执行目标代码。模拟测试验证执行契约，不证明真实模型的召回率、误报率或成本收益。真实模型评测与官方 judge 的运行方式见 [ReviewBench 评测说明](eval/reviewbench/README.md)。

通过 [GitHub Issues](https://github.com/waangzh/Rivu/issues)反馈问题时，请提供复现步骤、预期与实际结果、脱敏错误信息；贡献改动应附相关验证结果，不包含凭据、数据库或运行产物。

## 许可证

许可声明：MIT。仓库尚未附 `LICENSE` 文件，完整许可文本待补充。
