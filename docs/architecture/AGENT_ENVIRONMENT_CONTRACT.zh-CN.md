# Agent 环境接口

[English](AGENT_ENVIRONMENT_CONTRACT.md) · **简体中文**

## 通用性设计方法（2026-10-30）

目标是 **DSec 沙盒系统的通用性**。TB2.1、MBPP、SWE-Gym、OpenHands、
mini-swe-agent 和 MiMo 是需求来源及验证案例，不是核心模块的组织方式。
本节不新增统一 agent 框架，也不要求外部应用采用下文的 rollout 接口。

每次从应用中发现问题，按以下顺序处理：

1. 用执行事实描述需求，例如“同一实例的文件保留，但每条命令使用新 shell”，
   不用某个 benchmark、agent 或 trainer 的名字描述系统能力。
2. 明确需求属于执行、状态、制品、资源、网络、恢复还是部署，找出现有状态所有者。
3. 区分应用规则和系统保证。应用决定 prompt、步数、上下文、评分与训练样本接收；
   DSec 保证受理、隔离、执行结果、状态身份和回收事实。
4. 优先补现有协议和模块；只有无法表达的系统语义才增加版本化能力。
5. 写出与模型、agent 无关的验收，再用多个应用检查是否可复用。
   接口测试、真实 Linux 执行、完整应用运行和性能结论分别记录。

### 关键决策

| 决策 | 系统约束 | 实践中的来源 |
| --- | --- | --- |
| GEN-001：调用方拥有交互循环 | 直接使用公开 SDK 即可；`AgentEnvironment` 是可选组合，不是所有应用的必经入口。核心不解析 bash 代码块、不管理 Thinking/摘要，也不生成训练 token/logprob | TB2.1 的反馈与 TITO、OpenHands 的上下文与工具协议 |
| GEN-002：按能力匹配，不按应用分支匹配 | 创建时声明版本化执行能力，与环境 ID、执行模式和资源需求共同约束放置；API 支持该协议不等于每个 Node 都支持 | 多后端工具、文件和流式 SDK 的可移植性 |
| GEN-003：状态语义不能被统一接口抹平 | 文件持久与 shell 状态持久是两件事；FnCall 每次独立调用，Container 保留工作区，MicroVM 有自己的恢复边界。后端不支持时明确拒绝，不能换执行模式或回落宿主机 | 多步代码修复、MBPP 独立评测、跨 episode 分叉 |
| GEN-004：恢复按状态所有权分层 | Edge 负责实例与执行证明，控制面负责准入与原节点路由，应用负责自身状态；沙盒恢复不能证明外部 agent、模型轨迹或训练 checkpoint 同步恢复 | 断连对账、原生工具 epoch、SAO 训练 |
| GEN-005：预算与结果保留原始含义 | 排队、创建、命令、调用方等待、应用总预算及评分预算分别处理；连接超时不合成命令超时。核心返回执行与截断证据，应用决定其评分含义 | TB2.1 verifier、长轨迹与缺失反馈 |
| GEN-006：环境是可复用制品，写状态属于实例 | 核心接受发布的环境身份，保留 EROFS/OverlayBD/ublk、适用的 pmem/DAX 和本地/3FS 存储路径；不可变层共享，私有写入隔离。任务映射和依赖准备属于可选应用或发布流程 | 多任务镜像、安装依赖与重复 rollout |
| GEN-007：完成必须有证据 | 命令完成、任务得分有效、实例停止和资源退还分别证明；UNKNOWN 保留原身份并禁止副作用重放，不能折算成训练零分或自动释放资源 | 失败轨迹、官方 verifier、停机与租约回收 |

GEN-006 在环境派生时也适用。构建实例持有自己的私有写入，发布者独立指定
父环境版本及允许进入新版本的内容。发布失败时仍可使用原目录。子版本有独立环境身份，
发布者需要重新核验其执行能力。TASK-024 的直接 ext4 文件增量实现和适用范围见
[制品发布指南](../guides/ARTIFACT_PUBLICATION.md#从隔离构建沙盒发布文件增量)。

TASK-026 对 MicroVM 区分两个版本。发布者和调用方使用可移交清单的 SHA-256 标识制品；Node 和 Edge 使用包含本地路径的部署 revision 核对具体执行对象。控制面同时固定所选 Node 和部署绑定。重复请求、暂停与恢复沿原所有者路由。制品交付步骤见[发布指南](../guides/ARTIFACT_PUBLICATION.md#跨-node-交付同一-microvm-环境)。

### 能力声明与环境绑定

公开 `DSecClient.run_microvm/run_container` 增加可选的
`required_capabilities`。复用现有 [native 协议能力](../../src/dsec/contracts/native.py)、
Node 能力视图、PlacementEngine 和 IAM 准入，不增加独立的环境调度器。

- 目前接受 `native-sessions-files-v1`、`native-stream-v1`、
  `native-aether-chronus-v1`、`native-http-v1`；扩展自动要求基础 native 协议。
- `sandbox-requirements-v1` 和 `environment-requirements-v1` 表示 API/Node 理解
  创建需求和环境绑定，**不是执行能力本身**。SDK 在旧 API 上拒绝带需求的创建；
  放置排除不理解该契约的旧 Node。
- 需求按集合规范化，存入 IAM 请求身份。相同 ID 修改需求必须冲突；空需求保持
  旧请求摘要和 Edge wire。应用不能在创建请求中伪造 `environment_contract`；
  控制面从 watcher 选定环境契约后将其固定在 IAM receipt。
- Node 的执行协议能力取自实时 Edge health，而非只相信部署配置；派发前复核。
  如果 watcher 过期且当前 Node 不满足需求，仅在确认未派发、Edge 无 intent 时
  释放准入并拒绝。已派发或 UNKNOWN 的请求继续查询原所有者，不另选节点。
- 具体环境由 `EnvironmentView` 描述：环境 ID、后端、执行模式、部署 revision
  和明确声明的协议能力。同节点的其他环境具备某能力不能代替当前环境。
  环境目录未声明能力时按未知处理，不能从运行时名称或 Node 协议自动推断。
- 能力声明绑定到环境描述中的制品 pins；内核、启动盘、层、模式或配置改变，旧声明
  即失效。发布器只在验证源声明和字节后，为改写后的部署路径重新绑定。
  revision 是部署身份，包含路径和配置，**不是跨主机内容去重或 OCI 镜像摘要**。
- Node 派发前核对选定 revision。Edge 在新建意图进入日志前再次核对，避免 Node
  复核之后服务被替换而运行不同环境。绑定进入 Edge 创建摘要；已受理/完成请求
  仍按旧日志对账，不因新目录撤回环境而重放。绑定不会传给 guest 或 VMM。
- 这是**受信发布者的声明与制品绑定**，不是自动 guest 探测、形式化 guest ABI
  验证或程序依赖证明。pmem/DAX、网络策略、生产隔离及快照兼容性仍需相应证据，
  不能通过任意字符串或一份声明获得支持。运行时原有制品完整性检查保持不变。
- 这是直接 SDK 创建的能力声明；不改变可选 rollout API、FnCall 调用或旧任务插件。
  不提供自动选择执行模式，也不把 FnCall 转成持久会话。

`tests/unit/test_api_sdk.py` 与 `tests/unit/test_cluster_control.py` 覆盖旧端拒绝、
需求筛选、规范化与冲突、能力撤回、未知结果不重放以及旧摘要兼容。
这些控制面/传输夹具测试不等同于真实混合节点、旧 guest 或训练验收。
滚动部署时应先升级全部 API 副本；健康探测不是跨旧副本的协议保证。

2026-10-30 本地相关回归运行 205 项，203 项通过；2 项 Linux mountinfo/`/proc`
专属检查在 macOS 跳过。现有集群守卫工具的 14 个代码变异对照均按预期失败，
包括 MicroVM 与 Container 的最终环境守卫。文档链接与 diff 格式检查通过。
这些结果不是实机验收。

环境绑定进一步使用 [EnvironmentBinding](../../verification/EnvironmentBinding.tla)
建模两次请求、两个 revision、过期 watcher、Node 检查后发生环境变化和 Edge 拒绝。
`selected` 对应 IAM receipt 的环境绑定；`NodeCheck` 对应 Node 派发前复核；
`EdgeAccept` 对应 Edge 在日志受理前对固定目录的版本校验。
该模型假定目录和其制品 pins 在单个 Edge 生命周期中不可被重新解释；实际内容完整性
由原有目录和后端验证负责。模型不证明构建声明真实、二进制语义或无限规模活性。
`tools/check_concurrency_model.py` 要求安全模型通过，移除 Edge 守卫的负对照产生
`AcceptedRevisionPinned` 反例。`tools/check_cluster_refinement.py` 将该守卫分别映射到
MicroVM 和 Container 的本地 RPC 回归，移除检查时必须出现行为断言失败。
本地安全模型遍历 484 个不同状态通过；移除守卫的模型找到所需反例。
这是有界安全检查与指定代码守卫的对应证据，不是任意实现的完整形式化等价证明。

2026-10-30 的[独立 Linux 验收](../reports/DSEC_V01_INSTALL_ACCEPTANCE.md#generality-linux-20261030)
进一步通过 27 项实际执行检查和 77 项安装版定向回归。真实 Firecracker MicroVM 和
QEMU Container 经公开 HTTPS API、mTLS Node 完成按能力创建、二进制文件、持久会话、
流式输出与重连，FnCall 保持调用间独立语义。分别在 API 已准入但尚未派发、Node
已检查但尚未发送 Edge 创建 RPC 时，受控替换测试 Edge 的环境 revision；两次都
返回 AdmissionDenied、Edge 无创建 intent、IAM 配额释放。原 DONE 证明仍可查询。
这是所测两种 guest 的基础 native/stream conformance，不包括 HTTP/session 文件扩展、
全部故障窗口、跨节点或训练验收，也不把发布声明变成自动 guest 认证。

### 后续按共性缺口推进

既有能力不因本轮未重测而重新列为未开发。会话文件扩展已有 2026-10-12 的真实
MicroVM 验收；Edge 重启、执行中断、UNKNOWN 不重放已有 2026-10-10 和
2026-10-25 的真实运行记录；API 副本失效后的原请求查询和流重连也已有验收。
HTTP 已实现并有本地 C agent/SDK、超时和 POST 去重测试，当前记录未证明完整
HTTPS API → QEMU guest HTTP 的端到端验收。具体证据见
[执行与隔离契约](EXECUTION_ISOLATION.zh-CN.md)及[历史安装验收](../reports/DSEC_V01_INSTALL_ACCEPTANCE.md)。
后续只补明确缺失的组合证据；代码、协议、guest 或部署边界变化时重跑对应既有
测试，不重新实现同一能力。新故障必须写明触发条件、缺失保证和验收终点后才立项。

能力完成状态、证据和候选工作只维护在[项目台账视图](../PROJECT_STATUS.md)，
本契约不再维护一张平行的优先级/待办表。新语义先明确系统不变量，再在所选任务的
有限验收中选择有状态代码修复、独立代码执行等不同应用语义复核；已有能力按实际
变更定向回归，不要求每次迭代重新跑全部应用。外部模型是否得分与沙盒是否正确
是两组证据，不能互相替代。
本轮不新增 MiMo/mini-swe-agent 专用适配器，也不启动新训练或扩大模型管理职责。

## 可选 rollout 组合

`AgentEnvironment` 是任务与训练框架之间的边界，定义在
[dsec.rollout.environment](../../src/dsec/rollout/environment.py)，旧
[agent_environment.py](../../src/dsec/rollout/environment.py) 保留兼容入口。它不替代沙箱 SDK，也不依赖
OpenEnv 服务。

## 职责

| 层 | 负责的内容 |
| --- | --- |
| 训练框架的 agent loop | 模型采样、回复解析、多轮消息、token/logprob、RL 算法 |
| `TaskEnvironmentAdapter` | instruction、环境与资源选择、动作执行、可信评分 |
| `DSecAgentEnvironment` | 把任务生命周期接到持久 rollout worker |
| worker / sandboxd | 调度、沙箱、动作日志、暂停恢复、资源回收 |

DSec 管理沙箱资源。模型权重、KV cache、优化器、训练/推理显存和模型 offload
由外部训练框架与推理服务管理。沙箱准入可以依据节点的剩余资源和外部负载等待，
不会为取得资源而修改模型服务配置或卸载模型。

`DSecClient` 的正式实现位于 [sdk.client](../../src/dsec/sdk/client.py)，
它只接受 HTTPS API Server、IAM 凭据和项目，不从 `DSEC_SOCKET` 自动直连 Edge。
本地开发客户端为 [DevEdgeClient](../../src/dsec/sdk/development.py)；
`libdsec_compat` 与 `dsec.compat.libdsec` 保留这个开发实现的模块别名。
容器后端、制品校验和生命周期 journal 由
[容器 Edge](../../src/dsec/runtime/container_edge.py)管理；客户端不读取宿主制品或启动 Docker。
原 `run_container`、`attach_container`、执行/查询/停止调用经同一沙箱服务传输。
评分与动作请求仍由 worker 记账，不因 RPC 路径迁移而改变已有请求摘要。
明确的 `ServiceBusy` 表示未受理；SDK 不为带稳定 ID 的请求悄悄换号重试。
调用方可在确认未受理后重新提交，UNKNOWN 仍必须先对账。

```text
训练框架的 agent 循环
    → AgentEnvironment: reset → step* → evaluate → stop
    → TaskEnvironmentAdapter / RolloutAPIClient
    → API Server → 认证节点网关
    → 私有 rollout worker → Edge → microVM 或容器
```

## 生命周期

调用方为每个 episode 保存稳定的 `rollout_id`。

- `reset(policy_prefix)`：任务插件返回 `EnvironmentSpec`；创建或附着 rollout，
  等待调度，把任务 instruction 作为 user 消息接到消息前缀之后，返回持久对话。
- `step(action, policy_message=...)`：提交稳定的 `step_id`、`action_id`、动作类型
  与载荷，以及原始 assistant 消息，返回 `EnvironmentObservation`。
- `dialogue()`：查询持久对话并等待已提交动作完成；未知状态需要对账。
- `evaluate()`：返回含有限分数、评分器身份及证据的 `EnvironmentVerdict`。
- `stop()`：释放本 episode 的沙箱与租约。

`EnvironmentAction.shell(...)` 是 shell 动作的便捷构造函数；协议本身允许其他
`kind/payload`。是否接受动作由任务插件决定。接口不规定 bash 代码块格式、步数
或采样参数。

## 上下文与评分

每次动作应携带完整的原始 assistant 消息，包括未执行的文本；命令输出通过任务
插件成为下一轮观察。训练框架从返回的 `dialogue["messages"]` 获取消息，不能用
文本重建丢失的 token/logprob。

Miles 的 Qwen3.5 Thinking 适配器按[官方模板](https://huggingface.co/Qwen/Qwen3.5-4B/blob/main/chat_template.jinja)
从最后一个 `</think>` 之后提取正文，兼容代码块前重复的结束标记。若结束标记之间
存在代码块、工具调用或完成信号，或正文重新打开思考段，则拒绝解析，不能静默丢弃
候选动作。每轮仍只执行正文中的第一个 bash 动作；原始 assistant 回复和 TITO 不改写。
`model_outputs` 记录 `thinking_end_tag_count` 和 `thinking_boundary_normalized`，
episode 的 `agent_metrics.normalized_thinking_steps` 记录发生兼容处理的步骤。

### Shell 反馈

新 shell episode 的 worker 对话使用 `dialogue_feedback_version=2`。每条执行结果
作为一条 user 消息返回，内容是 `dsec.shell_observation.v1` JSON；训练框架应直接
使用 worker 返回的消息，不能再次只提取 `output`。非 shell 动作的观察仍由任务插件定义。

| 字段 | 含义 |
| --- | --- |
| `step_id`、`action_id` | 对应持久动作的身份 |
| `status` | `succeeded`、`failed`、`timed_out` 或 `unknown` |
| `exit_code` | 实际进程退出码；缺失为 `null`，不能补成 0 |
| `timed_out` | 执行层报告的命令超时；未提供为 `null` |
| `capture_truncated` | 执行层采集输出时是否截断；未提供为 `null` |
| `feedback_truncated` | 已采集输出是否因模型上下文预算再次截断 |
| `captured_output_chars`、`feedback_omitted_chars` | 已采集字符数、向模型省略的字符数 |
| `output` | 已采集的合并 stdout/stderr，最多保留 4000 字符 |

状态和截断标记始终保留。空输出也有完整结果，不再用 `(no output)` 代替执行状态。
长输出保留开头和结尾，并插入省略标记；完整已采集输出仍在动作日志中。
若执行层已经截断，反馈无法恢复没有采集的尾部，必须通过 `capture_truncated` 告知调用方。

只有明确的 `timed_out=true` 才判为命令超时；退出码 124 本身不足以证明超时。
命令超时与 RPC/连接超时不同：连接中断仍走 UNKNOWN、附着和对账流程，不能合成
一条“命令失败”反馈后自动重试。当前 shell 执行接口等待命令结束，不提供后台进程
句柄；不会把返回慢或没有输出解释成后台仍在运行。

反馈版本随 episode 持久保存。升级前已开始、没有版本字段的 episode 按旧版本 1
重建对话，后续动作也保持旧格式，以保护已有 session/TITO 前缀。新建或尚未开始
对话的 episode 使用版本 2。重连、动作去重和 worker 重启不会改变既有消息。

任务 instruction 来自任务插件，不由通用框架硬编码。TB2.1 插件调用任务原始
`tests/test.sh`，核对 CTRF 结果与完整性，再解释成二元奖励。缺失、无效或无法验证
的结果应报错并拒收样本，不能记作模型零分。

### Worker 评分插件

[评分契约](../../src/dsec/contracts/evaluation.py)只包含上下文、结果和插件接口，
不启动沙箱或加载任务代码。部署方给 `RolloutWorker(..., evaluators={id: plugin})`
注册可信实现；RPC 只能选择已注册的 ID，不能指定 Python 模块或加载代码。
插件的版本化 ID 在评分语义改变时必须更新。`validate` 在执行前检查任务、环境和参数；
`evaluate` 使用同一 episode 的沙箱，返回含有限数值 `value` 的 `EvaluationOutcome`。
`EvaluationFailure` 表示没有有效 verdict，其诊断证据保存在 worker journal。

调用方使用 `ScheduledSandbox.evaluate(evaluator_id, parameters, timeout_s=...)`。
worker 在执行前持久保存评分器 ID、JSON 参数及摘要；同一已完成评分返回原奖励，
换评分器或参数则拒绝。缺少评分器身份的旧完成记录由插件核对原奖励格式。
取消、错误或重启时未完成的多命令 verifier 保持 UNKNOWN，不能自动重跑或合成零分。
传输 timeout 只控制客户端等待时间，不改变任务规定的 verifier 上限。

TB2.1 的[官方评分插件](../../apps/tb21/src/dsec_tb21_case/worker_evaluator.py)随可选应用安装。
旧 `tb2_evaluate` 请求与 `--tb2-tasks-dir` 配置通过兼容桥注册该插件；不配置 TB2.1
时，导入和构造通用 worker 不加载该应用。旧计数任务保留单次 RPC 的证明对账语义。
TB2 adapter、manifest 验证和命令 PATH 的兼容桥仍有待后续迁移，当前不是完整任务代码拆分。

动作日志同时保存用户命令 `command` 和实际提交命令 `execution_command`。
重启对账使用后者，避免命令转换策略改变后重新解释已经执行的动作；旧动作没有该字段时，
按兼容转换规则核对原请求。动作去重继续基于原 step/action ID 与用户载荷。

### Episode 预算结束

Miles 的 DSec 适配器在沙箱就绪后开始 agent 预算，调度排队和 verifier 不消耗该预算。
Agent shell 的 `command_timeout_ms` 与官方 verifier 的 `verifier_timeout_ms` 独立。
TB2.1 verifier 上限从固定版本任务的 `task.toml` 读取；只有宿主 evaluator 的
`run_verifier_shell` 选择该上限，普通 agent 动作仍按命令上限校验。
请求超出范围会在执行前拒绝，并报告 scope、请求值和允许范围；这不表示测试已经超时。
到期后不再发起模型请求或执行新动作；在途 shell 命令的 timeout 受剩余预算限制。
已发出的模型请求允许返回完整响应，以保留真实 token/logprob，因此收尾可能超过预算；
`budget_overrun_seconds` 单独记录这部分时间，不承诺严格的墙钟终止。

到期轨迹以 `exit_status=timeout`、`end_reason=episode_timeout`、reward 0 正常返回，
`reward_source=episode_budget`、`dsec_budget_verdict=true` 标明评分来源。
不再运行任务 verifier，其诊断 `raw_reward/harness` 为 `null`，不能把预算零分表示为
官方测试成绩。接收该样本要求同一 episode 的完整 TITO 已保存、至少一条完整模型响应、
预算时间确实耗尽、无跨训练进程恢复；仍检查 Miles 原生 token/logprob 对齐。
零奖励样本保留在原 GRPO 分组中，不替换任务、不因预算到期停止其他 episode。
训练结果回写 ownership 记录，供进度与收尾审计区分预算结束、官方评分及未决错误。

传输超时、执行结果 UNKNOWN、丢失 TITO 和外部取消不属于预算零分。它们继续报错、
附着或对账，不能通过伪造 token、退出码或 verifier 结果来满足分组完整性。

## 恢复与隔离

遇到 `ScheduledOutcomeUnknown` 或 `UnresolvedAction` 时，保持原 rollout/action
ID，附着并对账；不得新建 ID 重放可能已经生效的副作用。明确的初始化失败会尝试
停止已创建的沙箱。

训练进程重启后，环境和对话可以恢复；若旧 TITO 已丢失，episode 不能用于策略
更新。模型轨迹的持久保存仍由训练框架负责。

`baseline_rollout_id` 允许从封存的准备态创建独立 episode。子实例使用私有可写
状态与独立历史；同一 episode 的暂停恢复保持原有身份。这两种复用语义不能混用。

## 实现与验证

- [通用计数任务](../../src/dsec_adapters/counter_dsec_environment.py)
- [TB2.1 任务插件](../../src/dsec_adapters/tb2_dsec_environment.py)
- [任务注册表](../../src/dsec_adapters/dsec_task_registry.py)
- [Miles agent 适配器](../../src/dsec_adapters/miles_dsec_agent_function.py)
- [接口回归](../../tests/unit/test_agent_environment.py)

正式入口经 API Server 与认证 Node gateway；单机 Unix socket 仅用于显式开发与
内部服务通信。生命周期与论文的 libdsec 思路相近，但本 agent 协议是工程扩展，
不声称与原始 DSec 实现的 apiserver/IAM 线协议兼容。

最新真实训练、恢复与分叉验证见
[GRPO 验收](../reports/DSEC_V01_GRPO_ACCEPTANCE.md)。短验收不代表全部 89 个任务、
长训练或多机环境已通过。


<a id="runtime-execution-boundary"></a>

### 运行时执行边界

核心的 `contracts.execution` 请求与结果类型、`runtime.sessions` 通道位于既有 reset/step/evaluate API 下层。Edge 分派层负责命令授权、作用域和截止时间校验、沙盒锁及自动恢复；通道只交换受限命令及其结果。MicroVM 沿用 guest-vsock 帧和 Edge 请求日志；Docker 沿用 guest 请求 ID 与查询证明。两种通道在丢回复时均不重试命令。已确认的命令超时以执行证据返回，传输不确定仍为 UNKNOWN。缺少超时证据的旧结果保留字段缺失。原生命令旧接口不变；会话、文件与有界流使用下述附加协议。两条路径均不管理策略或模型状态。

<a id="native-sandbox-sdk"></a>

### 原生沙盒 SDK

公共 SDK 经 API Server 和认证节点网关访问 Edge。Edge 调用共用的 C agent：MicroVM 通过原生 vsock 端口 5001，分层容器通过私有 UDS。端口 5000 保留原命令帧。`native-sessions-files-v1` 与 `native-stream-v1` 均为显式能力，新 SDK 不能在旧 guest 上静默模拟。升级 agent 须单独构建不可变制品。Rust OverlayBD/ublk 路径不变。

```python
from dsec.sdk import DSecClient

async with DSecClient(api_endpoints, token=iam_token, project=project_id,
                      ca_file=api_ca_file) as client:
    sandbox = await client.attach(sandbox_id)
    async with await sandbox.open_session() as session:
        await session.run_shell("cd /workspace; export MODE=test")
        result = await session.run_shell('pwd; printf "%s" "$MODE"')
        await sandbox.write_file("/workspace/input.bin", b"\x00\xff")
        assert await sandbox.read_file("/workspace/input.bin") == b"\x00\xff"
        async for event in session.stream("printf started; sleep 1; printf done"):
            if event["type"] in ("stdout", "stderr"):
                print(event["type"], event["data"])  # bytes; decode incrementally if needed
            else:
                print(event["result"])
```

每个会话具有持久的 `/bin/sh`、cwd 和环境变量。沙盒级 `run_shell` 仍为单次执行。同一 SDK 会话的命令串行，不同会话可并发，仅共享沙盒文件系统。另一句柄争用繁忙会话时收到明确的未准入证明，SDK 使用同一 ID 等待。guest 明确未准入时流采集器也会等待，最长 35 秒，`queue_wait_ms` 记录等待时长。取消排队中的流会持久记录意图并阻止分派。已分派流可能仍在途中，取消只重试精确匹配的 ID。`cancel_requested=true` 表示取消意图已受理，不能确认进程已终止。对已完成操作，取消返回 false 及终态；不会误伤随后占用 shell 的命令。不提供 PTY 或交互 stdin。命令的 stdin 为 `/dev/null`，Linux 在命令完成后清理已脱离的子任务。超时、取消、`exit` 或 shell 控制 FD 丢失会结束会话。后续调用报告 `NativeSessionReset`，不能静默创建替代 shell。

原生结果分别保留 stdout/stderr 及 `exit_code`、`timed_out`、`cancelled`、`truncated`、`session_reset`。未知退出码为 `null`。shell 参数仍受该环境的命令时限和代理策略约束。两条输出流的总量受调用方的选定上限限制，最高 1 MiB。

文件按 64 KiB 分块传输，单文件上限 64 MiB。一个 guest 最多预留 32 个未完成上传，合计 64 MiB。写入先暂存在目标目录，再通过 fsync/rename 发布。提前提交不会替换目标。重命名后若不能确认持久性则为 UNKNOWN。原子写替换指定的目录项，目标是符号链接时替换链接本身。读取则跟随 guest 符号链接，且要求目标为普通文件；跨块比较设备号、inode、大小、mtime 与 ctime，变化则拒绝，避免混合版本。`abort_file_write(path, transfer_id)` 可显式清理未完成上传。将稳定的写请求 ID 保存为传输 ID。

Edge 在既有请求日志中持久记录原生操作意图。相同 ID 和参数可附着或取回原结果，参数冲突则拒绝。丢回复或提交失败仍为 UNKNOWN，绝不重放。上传分块也使用稳定 ID。guest 侧不另建执行日志。

`session.stream(..., request_id=operation_id)` 使 Edge 在后台有界采集输出。订阅者凭返回的字节 `cursor` 读取事件，`session.events(operation_id, cursor=cursor)` 只重新附着而不重发。订阅者脱离不会取消执行。每条命令的事件暂存最多 8 MiB；溢出后继续排空 guest 输出，但丢弃后续输出事件，并在最终结果设置 `stream_truncated=true`。日志提交后才可见最终成功。`session.query(operation_id)` 读取同一日志。`session.cancel(operation_id)` 只作用于正在执行的精确命令，不会杀死后续命令。取消执行中的命令会重置其 shell，取消排队命令则保留当前会话和其他工作。

MicroVM 生命周期准入沿用持久台账。空闲会话可纳入 VM 快照；有活跃原生调用或流时，产生副作用前拒绝暂停。Edge 重启发现原生操作被中断时，退役该 VM 并保留 UNKNOWN，与原命令中断策略一致。重新附着空闲会话不创建 VM 或 shell。Edge 正常退出时先等有界采集器完结，再释放归属。不支持 Container 内存快照。旧 TB2 OpenEnv 镜像不会自动获得原生能力，应选择装有新 agent 的 MicroVM 或分层容器。

本地测试运行编译后的 agent 和真实 shell，覆盖 SDK → Edge → UDS、去重、提交故障、慢订阅者/脱离和有界输出。2026-10-10 的 Linux 安装版验收另验证真实 vsock、空闲会话快照恢复、分层容器部署、空闲/暂停状态下重启 Edge，以及原生流期间的 SIGKILL：请求变为 UNKNOWN，旧 VM 退役，拒绝再次提交。固定版本的 TB2.1 任务通过官方验证器，32 个固定代码的 MBPP episode 通过预期评分与回收检查。这些应用回归使用原有单次调用适配器，未将训练迁移到原生会话。[安装报告](../reports/DSEC_V01_INSTALL_ACCEPTANCE.md)记录了配置与边界。生产隔离、全任务正确性、高并发性能及所有存储组合仍需独立证据。

<a id="formal-concurrency-model"></a>

### 并发形式化模型

可执行的 [TLA+ 生命周期模型](../../verification/NativeLifecycle.tla)规定**目标并发契约**，尚不能据此断定运行时实现满足细化关系。独立的[信号模型](../../verification/ShutdownSignal.tla)揭示 `ns_stopping = ns_run(...)` 的读取、返回和赋值竞争窗口。模型不打入运行时包；只有贡献者及 CI 需要 Java/TLC。[官方 TLC 工具](https://github.com/tlaplus/tlaplus)穷举有限状态。[运行器](../../tools/check_concurrency_model.py)固定为 1.7.4，执行前核对 jar 的 SHA-256。

生命周期配置包含两个稳定请求 ID 和两个 VM 实例代（`MaxEpoch=1`，epoch 0 和 1）。Edge 与 guest 动作独立交错。启动许可、完成/失败回调及取消消息均可延迟，回调可重复观察；Edge 可在任意边界崩溃/重启。不设置深度截断、状态约束或对称性约简，不采用随机模拟。TLC 遍历该有限抽象的全部可达状态。扩大边界要重新检查，不能从两个请求推广到任意客户端数量。

权威状态为 Edge 的持久请求/生命周期记录，guest 动作不读取该记录。执行获准后才能发送启动许可，因此已准入但未开始的命令走运行中取消路径。排队取消只有在尚无许可时才能立即完成。运行中取消须等待接收端撤销或终止，不能撤销 shell 已产生的副作用。已提交的完成状态仍为完成。

`RequestStop` 关闭准入并进入 `STOPPING`。先前已准入的命令仍可能在停机期间启动。仅在确认 VM 死亡后提交 `STOPPED`。恢复会在旧 VM 死亡后启动新一代实例。修改状态的回调在写入边界须核实实例代身份；它与快照代数不同，也不能仅由可复用的 PID 推断。

| 属性 | 含义 |
| --- | --- |
| `AtMostOnce` | 所建模历史中，稳定请求最多启动一次。 |
| `TerminalNoNewExecution` | 已提交成功或确认取消后不再启动。 |
| `StoppedIsFinal` | 同一实例代内的迟到回调不能撤销 `STOPPED`；显式恢复会更换实例代。 |
| `StoppedIsQuiescent` | 已停止的沙盒没有存活 VM 或活跃命令。 |
| `PausedIsQuiescent` | 暂停时没有已获准或运行中的原生工作。 |
| `UnknownNeverReadmitted` | 不确定的请求不能再次准入。原命令即使结果 UNKNOWN 也可能已产生副作用。 |
| `StartIsFenced` | 启动只交付给身份匹配且存活的实例代。 |
| `CallbackIsFenced` | 来自旧实例代的失败回调不能修改当前状态。 |
| `StopIsMonotonic` | 信号请求停机后，普通返回值处理不能清除停止标志。 |

模型假设 Edge 独占管理，准入及结果转换原子持久化，不自动重发已准入命令；传输对每个已提交启动至多交付一次，进程终止确认正确。客户端重试附着到现有日志。`seen`、执行计数和终结计数只观察历史，不要求新增 guest 执行日志。模型未覆盖拜占庭 guest、文件系统持久化内部过程、任意 shell 副作用、多节点、文件传输竞争、会话互斥、事件暂存正确性、网络隔离或资源租约清理。只检查安全性，不承诺公平性、最终交付或最终完成。终态/静止状态允许停顿，因此关闭了死锁检查，**不能据此证明无死锁**。完整线性一致性证明还需要调用与响应历史及细化映射。

| 模型边界 | 代码对应点及剩余边界 |
| --- | --- |
| 先准入后启动，按请求 ID 去重 | 现有 `RequestJournal` 和 `NativeJobs.start`；EBUSY 证明 guest 未接纳。须覆盖 Edge 接受后、guest 准入前的窗口。 |
| 排队取消、运行中取消与确认终态 | `NativeJobs.cancel` 在采集器控制锁下持久记录待取消流意图。排队意图阻止分派；已分派在途时只重试对精确操作 ID 的取消。意图确认和最终结果分开。 |
| 暂停时检查活跃工作 | `native_operation` 与 `LifecycleController.pause` 已在沙盒锁下登记并核对原生工作。 |
| 停止完成与拒绝迟到回调 | `native_operation` 捕获持久化的进程实例代；迟到的 UNKNOWN 回调仅能退役同一 RUNNING 实例代。`LifecycleController.fail` 保留 STOPPED。Container 活跃操作登记与停止准入共用生命周期锁，操作直到原生 I/O 结束才解除登记。 |
| 中断转 UNKNOWN，显式恢复 | 现有台账和请求日志恢复流程会退役被中断的 VM。`native_incarnation` 在恢复启动替代进程前递增并持久化，与快照代数独立。旧回调不能使新进程失败或修改其状态。 |
| 信号单调性 | `ns_session` 仅当返回值要求停止时才写入 `1`，否则不写。真实 C agent 的轨迹检查覆盖此处，并要求拒绝旧赋值行为。读改写 `stop |= returned` 也会与信号竞争。 |

不安全变体分别破坏排队取消、停止终态、旧实例代隔离、UNKNOWN 不重放、启动隔离和信号单调性。CI 要求精确的预期不变量违例和 TLC 反例，仅有非零退出码不够。生命周期变体检查的是目标契约的变异；以下定向模型还会重放真实实现观测，并要求指定代码变异在同一回归中失败。

使用 Java 11+ 运行 `python tools/check_concurrency_model.py --fetch --out /tmp/dsec-model-check-unique`，也可通过 `--jar` 指定固定版本的 jar。不能覆盖已有输出目录。每次保存模型/配置副本、原始反例、状态数、工具和模型哈希及 `report.json`。搜索不完整则失败。下述检查映射指定的生命周期动作，并以确定性屏障重放反例顺序。它们补充模型穷举，尚未构成完整运行时实现细化证明。

<a id="worked-implementation-correspondence-shutdown-signalresult-race"></a>

#### 实现对应示例：停机信号与返回值竞争

使用 Java 11+ 和 C 编译器运行 `python tools/check_shutdown_refinement.py --fetch --out /tmp/dsec-shutdown-check-unique`。[对应关系检查工具](../../tools/check_shutdown_refinement.py)编译真实 `guest_native.c`，运行持久 shell 的命令路径，并借助[编译期探针](../../verification/native_shutdown_probe.h)在指定边界投递 SIGTERM。常规构建不含探针及其环境变量。处理器以固定长度、异步信号安全的写入记录观测；Python 不负责判断哪些状态转换合法。

| 观测点 | 模型投影 |
| --- | --- |
| 实际调用 `ns_run` 前 | `phase=RUN`、实际 `ns_stopping`、初始返回值和信号标志 |
| `ns_run` 返回后、应用其结果前 | `phase=ASSIGN`、实际返回值和停止标志 |
| 真实 SIGTERM 处理器内，设置标志后 | `Signal`：实际停止标志和信号标志，保留当前阶段 |
| 调用方应用结果后 | `phase=IDLE`、实际停止标志和缓存的返回值 |

`Return` 允许两个布尔值，因为超时、退出和 shell 重置即使没有 SIGTERM 也可要求停止。这修正了原先过窄的抽象（`returned=stop`），`StopIsMonotonic` 保持不变。安全调用方为 `if (stop_run) ns_stopping=1;`，普通返回值处理不会写零。

[ShutdownReplay 轨迹封装](../../verification/ShutdownReplay.tla)扩展同一个 `ShutdownSignal` 模块，要求每一对相邻观测状态满足原有 `Next`。若下一观测不合法，`TraceConforms` 拒绝该前缀。成功不可仅从事件名推断。五种受控场景覆盖无信号、返回前收到信号、返回后应用结果前收到信号、应用后收到信号，以及显式 shell 退出。

必需的负对照只将结果处理语句替换为历史上的 `ns_stopping=stop_run;`。真实轨迹符合不安全模型且违反 `StopIsMonotonic`，同一轨迹在安全模型中被 `TraceConforms` 拒绝。因此该检查能发现旧赋值被重新引入，不能靠任意编译成功的 agent 通过。如果标注的代码边界消失或被重构，工具拒绝猜测映射。实现或模型发生变化时，须审查映射并重新生成轨迹。

每次运行保存原始和变异 C 源码、构建日志、二进制/探针/模型哈希、原始二进制观测、投影后的 JSON 状态、生成的重放输入和 TLC 判定。2026-10-10 本地验证通过五条正向轨迹和两项必需的负向检查。所记录前缀只覆盖命令返回与信号交错窗口，未覆盖指令级的所有交错、完整运行时细化、停机延迟或活性。常规原生 SDK/agent 测试仍独立验证执行与清理。


<a id="implementation-correspondence-queue-callbacks-and-container-admission"></a>

#### 实现对应示例：队列、回调和容器准入

运行 `python tools/check_native_race_refinement.py --fetch --out /tmp/dsec-native-race-check-unique`。[标准回归套件](../../tests/unit/test_native_races.py)运行真实 `NativeJobs`、请求日志和生命周期方法。队列测试使用编译后的 C agent 和真实 Unix 套接字命令。宿主进程及容器驱动为带观测点的测试夹具，属于本地契约验证。

| 反例顺序 | 实现边界与确定性回归 |
| --- | --- |
| 入队 → 取消返回 false/PENDING → 分派并执行 | [QueueCancellation](../../verification/QueueCancellation.tla)：`before_dispatch` 屏障挂起采集器；取消必须持久记录意图，返回 true/PENDING，以 queued/cancelled 结束，且不能创建命令标记文件。重新附着同一 ID 不能执行命令。 |
| 已获分派许可 → 取消返回 ENOENT → guest 准入 | 传输屏障在发送前挂起真实流。精确 ID 的首次取消到达 guest，收到 ENOENT。放行分派后，取消必须重试同一 ID 并取得已取消终态，不能重试执行。 |
| 提交结果 → 清理抛错 → 迟到的取消 | 提交后注入异常须保留持久 DONE。取消返回 false/DONE，证据和命令副作用不变。 |
| 准入 → 停止完成 → 旧传输报错 | [NativeCallbackFence](../../verification/NativeCallbackFence.tla)：挂起原生操作，调用真实 stop，再投递 UNKNOWN。STOPPED 和已完成的资源回收必须保持终态。 |
| 第 N 代准入 → 退役 → 恢复至 N+1 代 → 来自 N 的回调 | 真实恢复路径在启动替代进程前递增并持久化 `native_incarnation`。放行旧 UNKNOWN 回调后，新进程仍为 RUNNING、存活且未被修改。 |
| 两个原生读者 → 停止或所属端关闭 | [ContainerNativeGate](../../verification/ContainerNativeGate.tla)：两个原生上下文并存。停止在生命周期日志准入前返回 ServiceBusy，所属端关闭也返回 ServiceBusy。两者释放后允许停止。 |
| 停止已锁定准入 → 新原生操作 | 在真实生命周期锁内挂起带观测点的后端停止方法。新原生准入返回 ServiceBusy 且无法到达端点。停止完成后继续拒绝原生工作。 |

[轨迹封装器](../../verification/RaceReplay.tla)将每一对相邻状态与原模型 `Next`（或无状态变化的观测）核对。Python 仅投影观测。队列投影取自实际阶段、取消 Event、持久响应和标记文件；回调投影取自实际生命周期状态、捕获/当前实例代及活跃操作表；容器投影取自已准入 ID、活跃操作移除、后端停止入口/完成和准入拒绝。历史监视量记录旧回调本身是否退役了 RUNNING 的替代实例。它不要求替代实例永远健康，无关故障仍是合法行为。TLC 揭露了初始不变量过强的问题，修正后未隐藏或排除独立故障。

四个负对照重新引入排队取消丢失、移除回调检查（停止终态有两道检查），或移除容器停止时的活跃操作检查。这些是经审查的代码变异。每项均须在同一测试中产生行为断言失败，符合不安全模型，违反指定属性，并让安全模型拒绝**同一条轨迹**。源码边界漂移、证据缺失、测试跳过、解析或准备/清理出错均导致运行器失败。保留源码副本和哈希、原始/变异代码、JSON 观测、回归日志、生成的 TLA+ 输入及配置、反例和汇总报告。CI 同时要求穷举模型检查和真实 C 信号检查通过。

API 的取消成功表示意图被接纳，不能保证进程已终止。在途命令可能先于取消执行，其日志持有最终结果。排队取消阻止后续发送。中断后未决的意图仍按 UNKNOWN 核对，不会标为 CANCELLED 或自动重放。原生活动未结束时，可以稍后重试 Container stop，与原有容器单次执行语义一致。

2026-10-10 本地验证覆盖七项确定性回归、五条安全实现轨迹和四条必需的故障代码轨迹。对应运行器共 22 项检查。完整模型在没有公平性或时间假设的条件下枚举有限状态。结果不证明所有指令级竞争、全系统线性一致性、最终取消、物理子进程必然终止、文件系统/租约清理或 Linux 部署通过。以下独立 Linux 验收验证指定运行时改动；更广的组合及生产发布有各自门槛。


<a id="real-linux-acceptance-2026-10-10"></a>

#### Linux 实机验收（2026-10-10）

[Linux 验收工具](../../tools/verify_native_races.py)在实验机上使用候选 wheel 安装版通过七项受控检查，覆盖 VM 和容器排队取消且不发送执行、分派在途时精确 ID 取消、停止完成后收到迟到的 UNKNOWN、真实快照恢复后收到旧实例代回调，以及容器原生操作和停止的两种准入顺序。容器准入到达真实原生 agent。回调测试执行真实 guest 命令，然后在宿主屏障处故意扣留或丢弃响应。使用真实 Firecracker 进程、快照文件和 Linux pidfd 身份验证与接管。EROFS 启动和层制品不可变，私有可写盘彼此隔离；恢复的 guest 在 Edge 接管前后均可成功执行命令。旧回调没有修改其空闲截止时间。

已安装核心 wheel 的 SHA-256 为 `4e6851aaa5788423dd904f442c0760d7c7bd8f0478b4723fd71f2ba26f686769`，实机执行源码清单为 `c7b83a4479468b08c301d11eb76d5ff1f6c55b543bf04a069877942904c96c8b`，guest C 源码为 `6df612523d2e39dfd188955b5155bc960f91dd2991b7a565e8f3ad7b2bcc59fb`。原始报告和日志位于专用验收目录 `/home/xiaoxiaohu/dsec-race-20261010`。实机判定为 `runtime-r4/report.json`，模型、信号、变异与 Linux 单元测试证据在 `evidence/`。保留更早的失败运行。发现的两个 harness 缺陷为：同 ID 流重新附着时默认参数冲突，以及两个 Edge 所属端同处一个 OS 进程时原始子进程句柄未回收。修复未放宽运行时守卫。接管的 VMM 停止后，harness 仅回收精确对应的原始 Popen 子进程，仍要求 `/proc` 消失。

Linux 重跑了 16 项穷举模型检查、七项 C 信号对应检查和 22 项实现/变异检查。全套单元测试 285 项，284 项通过，另一个旧实验模块别名测试不适用于导出树。仅为收集应用相关回归，用同一源码快照构建了可选 TB2.1 测试包。实机验收无需下载 TB2.1 任务，也不需要模型或 GPU。六条真实后端轨迹经 `check_native_race_refinement.py --observed /path/to/report.json` 在同三份原始 TLA+ 规格上重放。

清理检查确认六个 VMM 进程实例代全已消失，无残留自有 Docker 容器、私有目录、原生活动或清理错误。验收未重启已有 Docker/Grafana/Prometheus 或 DSec 用户服务。这是定向单主机契约验证；无界线性一致性、全部取消交付窗口、任意故障下子进程必然终止、3FS/ublk 恢复、网络隔离、调度器租约正确性和性能结论仍不在范围内。

<a id="aetherchronus-execution-extensions"></a>

## aether/chronus 执行扩展

[执行与隔离契约](EXECUTION_ISOLATION.zh-CN.md)记录当前 Linux 代理/会话的职责划分及尚未就绪的生产路径。Edge 与 guest 都需确认能力后，才能使用 `native-aether-chronus-v1` 会话文件和可选的 `native-http-v1` HTTP 请求。沙盒范围的旧文件接口仍兼容。`session.request` 返回二进制正文、原始响应头、HTTP 状态、超时/截断和会话重置标记。结果未知时只允许查询。扩展沿用现有 Edge 请求日志。


<a id="rollout-api-ownership-and-verification"></a>

### Rollout API 的状态归属与验证

`RolloutAPIClient` 经 API 复用既有 `ScheduledSandbox` 语义；`ScheduledDSecClient` 保留为本地开发或内部传输。TB2.1/Miles 与 MBPP/verl 使用同一 API 配置，并保留策略消息、step/action 身份、奖励及证据。任务与训练代码不进入核心 API。worker 仅执行运维人员已注册的评分器。

IAM 以公共 rollout ID 为稳定准入键，固定规范化创建参数及节点。网关把项目/rollout ID 哈希为 worker ID，在产生副作用前验证 worker 的任务、配置、资源及物理归属。返回时只映射身份字段为公共 ID，不改写模型消息或命令输出。API 不存储轨迹，worker 日志持有权威执行记录。

`RolloutDispatch.tla` 在一个已准入 episode、延迟到达、一条 worker 记录及独立清理证明的边界下建模。`Dispatch` 对应唯一一次发送 worker 创建前的 IAM CAS，`ReachWorker` 对应持久 worker 预留，`ProveCleanup` 对应匹配的沙盒状态，`Release` 对应 IAM 的 CAS。受控回归覆盖并发重复创建、已提交回复丢失、分派后尚无 worker 记录的窗口、命名空间隔离、保留验证器失败及拒绝无凭据释放。规定的代码变异重发创建或移除清理守卫，必须让对应回归失败；模型还须产生重放和无证明释放反例。既有 `ClusterAdmission` 继续验证配额与不可变调度。两份模型均未证明任意实现细化、无哈希碰撞或集群可用性。
