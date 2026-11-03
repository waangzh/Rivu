# ReviewBench 本地评测

直接复用 Rivu 审查服务和 ReviewBench 官方评分器，用于个人观察效果，不需要 Docker、榜单注册或 Harbor。默认只跑一个 PR，逐个执行；某个样本失败会记录原因并继续。

## 准备

在仓库根目录执行：

```sh
cd backend
uv sync --extra test
git clone --depth 1 https://github.com/review-bench/ReviewBench.git .rivu/reviewbench
npm --prefix .rivu/reviewbench ci
```

需要 Git、Node.js 20+ 和可用的模型配置。Reviewer 默认使用 `backend/.env` 中的 Provider、模型和资源上限。首次评测会在独立运行目录内创建数据库并执行迁移，不修改平时使用的数据库。

## 先生成一个 PR 的审查结果

仍在 `backend/` 执行：

```sh
uv run python -m app.evaluation.reviewbench \
  --benchmark-dir .rivu/reviewbench \
  --run-dir .rivu/evaluations/first-reviewbench \
  --limit 1 \
  --no-judge
```

脚本先从官方镜像获取固定 Base/Head，失败时尝试上游仓库；只读取已提交源码，不执行样本代码、安装样本依赖或发布 GitHub 评论。PR 标题、正文作为不可信作者声明随本地输入保存。

输出保存在运行目录：

| 路径 | 内容 |
| --- | --- |
| `findings/` | 官方 judge 可直接读取的标准结果 |
| `summary.json` | 成功、部分完成、失败数量，Review ID、用量、未决与跳过项 |
| `scoring/results.json` | 官方汇总分数，执行 judge 后生成 |
| `scoring/results.details.json` | 官方逐条判定，执行 judge 后生成 |
| `reviews.db`、`artifacts/` | 本次评测的原始审查状态和证据 |

## 调用官方 judge

Judge 使用自己的 provider、模型 ID 和对应环境变量；Reviewer 的 `.env` 不会自动成为 Node 评分器的凭据。可以使用已有的可用模型，不要求与官方榜单一致。模型需要支持工具调用。

例如配置好 `DEEPSEEK_API_KEY` 后：

```sh
uv run python -m app.evaluation.reviewbench \
  --benchmark-dir .rivu/reviewbench \
  --run-dir .rivu/evaluations/first-reviewbench \
  --judge-only \
  --judge-provider deepseek \
  --judge-model deepseek-chat
```

也可首次运行时直接提供 `--judge-provider`、`--judge-model`，省略 `--no-judge`，一次完成审查和评分。`--judge-concurrency` 默认 1。自定义网关或模型使用官方支持的 `PI_CODING_AGENT_DIR/models.json`，具体配置见官方 `docs/OFFLINE_EVAL.md`；其中 `apiKey` 填环境变量名称，不写真实密钥。

Review 和 judge 都会产生模型调用费用。Judge 失败后用相同 `--judge-only` 命令重试，不会重新执行 Reviewer；评分器自行管理 checkpoint。

若本轮导出的 findings 全为空，脚本自动传入官方要求的 `--allow-empty`，照常计算召回率；这不会把部分完成的审查改成完整成功。完全没有结果文件时仍提示先检查失败记录。

## 选择样本和解释结果

- `--limit 5` / `--limit 25`：按官方测试清单顺序选取前几个 PR，默认 1。
- `--pr 3`：只跑索引为 3 的 PR，索引从 0 开始。
- `--set full --limit 219`：选择完整集；个人试跑不需要全量。
- `--model ...` / `--depth quick|standard|deep`：覆盖 Reviewer 模型和深度。
- `--timeout 300`：设置审查期限，沿用现有服务的资源上限。

每次重新审查使用新运行目录，避免旧结果混入；失败样本可用 `--pr` 在新目录单独重跑。只有评分可复用已有运行目录。

导出正式报告中的 confirmed Finding，包括核验确认的既有问题；partial 运行中已确认的问题也可评分。Base-only 或没有 HEAD 定位的 Finding 暂时跳过并计数，不伪造行号。未决候选不导出，失败和取消不生成“成功且无问题”的结果。

读分数时同时查看 `summary.json` 的实际样本量、部分完成与跳过数量，以及官方明细。Grounded 指标衡量与 golden 的匹配；augmented 指标加入对未匹配问题的分类。当前 Rivu 偏重行为缺陷，文档、测试和维护性类别的召回可能与通用 reviewer 不同。
