<a id="working-in-this-repository"></a>

# 在本仓库中工作

[English](AGENTS.md) · **简体中文**

提出或继续一项工作前，先阅读 [项目状态](docs/PROJECT_STATUS.md)和贡献指南中的[工作管理](CONTRIBUTING.zh-CN.md#work-management)。当前状态只在 `project/registry.json` 维护；使用 `python tools/check_project.py --write` 生成状态页。

先选定任务 ID，核对历史证据，说明本次增量及停止条件，再开始实现或实验。最新报告中未提及某项历史验收，不影响原结论。缺少某个组合的证据也不等于功能尚未实现。复用权威实现与已有验收工具。重开已关闭事项，需要指出发生变化的边界或可复现的缺陷，并引用原记录。同时只执行一项任务，候选项不会自动转为执行中。

架构决策维护在已有 GEN/CPH 记录中。核心负责沙盒；模型、提示及 RL 算法由调用方管理。公开应用使用 HTTPS API，UDS 供开发及内部服务使用。保留无关工作和宿主服务。关闭所选任务时记录有明确范围的证据、剩余限制，如果创建了资源，还要证明已回收。台账或文档变更须运行 `python tools/check_project.py` 和 `python tools/check_docs.py`，其他检查按实际改动边界选择。
