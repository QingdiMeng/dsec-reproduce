# 文档目录

[English](README.md) · **简体中文**

第一次使用 DSec，可先按[快速开始](guides/QUICKSTART.zh-CN.md)运行一个固定回复的 episode，再选择下面的应用。如果你负责部署，请从集群指南和宿主配置开始。应用通过 HTTPS API 接入；直接连接套接字供开发和内部服务使用。RL 示例通过 rollout API 运行。验收报告中的分数对应当时记录的版本和配置。

## 使用指南

| 文档 | 什么时候读 |
| --- | --- |
| [快速开始](guides/QUICKSTART.zh-CN.md) | 安装核心包、准备测试 guest，并验证独立实例 |
| [应用案例](guides/USE_CASES.zh-CN.md) | 选择工作负载，查看应用入口、依赖和当前限制 |
| [宿主配置](guides/DSEC_HOST_CONFIGURATION.zh-CN.md) | 配置路径、运行账号、存储与资源预算 |
| [高可用集群预览](guides/CLUSTER_DEPLOYMENT.zh-CN.md) | 部署 IAM、API 副本和节点，了解调度与节点状态监测 |
| [集群管理控制台](guides/CLUSTER_CONSOLE.zh-CN.md) | 在同源 Web 界面查看有权访问的资源，查询结果不确定的请求 |
| [服务部署](guides/SERVICE_DEPLOYMENT.md) | 部署和维护沙盒守护进程与 rollout worker |
| [制品发布](guides/ARTIFACT_PUBLICATION.md) | 准备并发布不可变的环境制品 |
| [RL 框架接入](guides/RL_FRAMEWORK_ADAPTERS.md) | 了解训练框架的职责、Miles 接入和历史 GPU 实验 |
| [Terminal-Bench 2.1](../apps/tb21/README.zh-CN.md) | 准备所选终端任务及镜像，注册环境并调用任务自带的验证器 |
| [MBPP + verl](../apps/mbpp/README.zh-CN.md) | 用 Python 沙盒计算奖励，对 Qwen3.5-2B 运行一个完整 epoch 的 GRPO，比较训练前后效果 |
| [SWE-Gym](../apps/swegym/README.zh-CN.md) | 通过 API 对固定版本的仓库运行基线和参考补丁，调用官方评分器，并用单题 SAO 试验验证训练流程 |
| [OSWorld](../apps/osworld/README.zh-CN.md) | 在 Ubuntu FullVM 中回放桌面操作，获取截图并按官方指标评分 |
| [OpenHands 适配器](../integrations/openhands/README.zh-CN.md) | 接入上游编程 Agent，检查执行能力和资源归属 |

## 架构与契约

| 文档 | 内容 |
| --- | --- |
| [模块化重构设计](architecture/MODULAR_REFACTOR_DESIGN.md) | 已确认的模块边界、与论文的对应关系、迁移进度和验收条件 |
| [Agent 环境契约](architecture/AGENT_ENVIRONMENT_CONTRACT.zh-CN.md) | 从工作负载推导系统要求，声明执行能力、管理任务生命周期，并在中断后找回操作 |
| [高可用控制面设计](architecture/CLUSTER_CONTROL_DESIGN.zh-CN.md) | 各组件的职责、故障边界和验收证据 |
| [沙盒守护进程](architecture/SANDBOX_DAEMON.md) | 请求持久化与沙盒生命周期管理 |
| [原生执行与隔离](architecture/EXECUTION_ISOLATION.zh-CN.md) | aether、chronus 的职责，兼容性和 QEMU 宿主隔离的验收范围 |
| [Docker 与 DSec 对照协议](architecture/DOCKER_DSEC_BENCHMARK_PROTOCOL.md) | 对齐任务与缓存条件，统计完整后端的资源开销 |

## 验收与历史实验

报告保留当时的实验机路径和原始证据引用。其中标为历史来源的文件可在发行包之外。部署当前版本请使用上面的指南。

| 文档 | 验证了什么 |
| --- | --- |
| [安装验收](reports/DSEC_V01_INSTALL_ACCEPTANCE.md) | 在新虚拟环境中安装并检查运行时 |
| [GRPO 验收](reports/DSEC_V01_GRPO_ACCEPTANCE.md) | 短程真实模型训练、快照、分叉和资源回收 |
| [完整性与成本](reports/DSEC_VERITY_PILOT_REPORT.md) | 验证器制品保护和完整后端资源开销对照 |
| [MBPP + verl 实验记录](reports/MBPP_VERL_FIRST_USE.zh-CN.md) | 首次使用遇到的问题、执行并发、187 次 GRPO 更新及 500 道留出题的前后测 |
| [SWE-Gym 实验记录](reports/SWEGYM_FIRST_USE.zh-CN.md) | 首次接入的过程、运行配置和评分证据 |
| [路线图](../ROADMAP.zh-CN.md) | 当前实现边界与后续工作 |

## 项目

- [项目状态与任务台账](PROJECT_STATUS.md)由统一台账生成，列出已实现的能力、证据适用范围和待选工作。
- [路线图与论文对照](../ROADMAP.zh-CN.md)
- [贡献指南](../CONTRIBUTING.zh-CN.md)
- [许可证](../LICENSE)和[第三方声明](../THIRD_PARTY_NOTICES.zh-CN.md)
