# 审查结果导出

导出只读取已保存的终态审查结果，不调用模型、不执行目标仓库代码、不发布 GitHub 评论。

## 接口

- `POST /api/reviews/{task_id}/export`：生成并保存导出文件，返回下载响应。
- `GET /api/reviews/{task_id}/export/{filename}`：读取已保存的导出文件。

请求体支持 `format`（`csv`、`json`、`markdown`）、`filename`、`severities`、`paths`、
`page` 与 `page_size`。仅已完成、部分完成、失败或取消的任务可以导出。

## 格式

- CSV：适合表格工具，列为路径、行号、严重度、标题、说明与归因。
- JSON：保留结构化字段，便于二次处理。
- Markdown：与审查报告一致的表格视图。

## 边界

导出文件保存在本地 artifact 目录的 `exports/` 子目录。导出不会改变审查结论、
覆盖声明或发布状态；下载响应也不代表发现已被发布到 GitHub。
