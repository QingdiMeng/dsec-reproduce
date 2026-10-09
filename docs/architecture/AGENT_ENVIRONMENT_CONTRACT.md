# Agent 环境接口

`AgentEnvironment` 是任务与训练框架之间的边界，定义在
[dsec.rollout.environment](../../src/dsec/rollout/environment.py)，旧
[agent_environment.py](../../agent_environment.py) 保留兼容入口。它不替代沙箱 SDK，也不依赖
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
`libdsec_compat` 与 `dsec.compat.libdsec` 保留同一实现的模块别名。
容器后端、制品校验和生命周期 journal 由
[容器 Edge](../../src/dsec/runtime/container_edge.py)管理；客户端不读取宿主制品或启动 Docker。
原 `run_container`、`attach_container`、执行/查询/停止调用经同一沙箱服务传输。
评分与动作请求仍由 worker 记账，不因 RPC 路径迁移而改变已有请求摘要。
明确的 `ServiceBusy` 表示未受理；SDK 不为带稳定 ID 的请求悄悄换号重试。
调用方可在确认未受理后重新提交，UNKNOWN 仍必须先对账。

```text
Trainer agent loop
    → AgentEnvironment: reset → step* → evaluate → stop
    → TaskEnvironmentAdapter / ScheduledDSecClient
    → rollout worker → sandboxd → microVM or container
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

- [通用计数任务](../../dsec_adapters/counter_dsec_environment.py)
- [TB2.1 任务插件](../../dsec_adapters/tb2_dsec_environment.py)
- [任务注册表](../../dsec_adapters/dsec_task_registry.py)
- [Miles agent 适配器](../../dsec_adapters/miles_dsec_agent_function.py)
- [接口回归](../../tests/unit/test_agent_environment.py)

当前实现使用单机 Unix socket。生命周期与论文的 libdsec 思路相近，但本 agent
协议是工程扩展，不声称与论文多机 apiserver/IAM 线协议兼容。

最新真实训练、恢复与分叉验证见
[GRPO 验收](../reports/DSEC_V01_GRPO_ACCEPTANCE.md)。短验收不代表全部 89 个任务、
长训练或多机环境已通过。


### Runtime execution boundary

The core `contracts.execution` request/result values and `runtime.sessions`
channels sit below the existing reset/step/evaluate API. Edge dispatch owns
command authorization, scope/deadline validation, the sandbox lock and automatic
resume; a channel only exchanges the bounded command and its result. The
microVM keeps its existing guest-vsock bytes and Edge request journal, while
Docker keeps its guest request IDs and query proofs. Neither channel retries a
command after a missing reply. A completed command timeout is returned as
execution evidence; transport uncertainty remains UNKNOWN. Legacy results with
no timeout evidence retain the missing field. The legacy command boundary
remains unchanged. Native sessions, files and bounded streaming use an additional
protocol described below. Neither path manages policy/model state.

### Native sandbox SDK (local implementation, real-VM acceptance pending)

The Python SDK and Edge call one shared C agent, through native vsock port 5001
in a microVM or a private UDS in a layered container. Port 5000 retains the
original command framing. `native-sessions-files-v1` and `native-stream-v1`
are explicit capabilities; a new SDK cannot silently emulate them on an old
guest. Agent upgrade is a separately built immutable artifact. The Rust
OverlayBD/ublk path is unchanged.

```python
from dsec.sdk import DSecClient

async with DSecClient(socket_path) as client:
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

Each session has a persistent `/bin/sh`, cwd and environment. `run_shell` on
the sandbox remains one-shot. Commands on one SDK session serialize; different
sessions can run concurrently and share only the sandbox filesystem. A second
handle contending for a busy session receives provable non-admission and the
SDK waits with the same ID. A stream collector also waits on explicit guest
non-admission, for at most 35 seconds; `queue_wait_ms` records that wait.
Cancellation reports `cancel_requested=false` if that exact command is not
currently admitted; it does not cancel a different command occupying the shell.
There is no PTY or interactive stdin. Commands receive `/dev/null` stdin;
detached child jobs are cleaned up after a command on Linux. Timeout, cancel,
`exit`, or loss of the shell's control FD ends the session. A subsequent call
reports `NativeSessionReset`, rather than silently creating another shell.

Native results retain separate stdout/stderr plus `exit_code`, `timed_out`,
`cancelled`, `truncated` and `session_reset`. An unknown exit code is `null`.
Shell arguments still use the environment's command deadline and proxy policy.
Output is bounded by the caller's limit, at most 1 MiB across both streams.

File transfers use 64 KiB chunks and a 64 MiB file limit. A guest reserves at
most 32 unfinished uploads and 64 MiB in aggregate. Writes stage in the target
directory and publish with fsync/rename; a premature commit cannot replace the
target. A rename whose durability cannot be confirmed is UNKNOWN. Atomic write
replaces the named file, including a symlink, rather than following that
symlink. Reads follow guest symlinks and require a regular file; they compare
device/inode/size/mtime/ctime across chunks and reject a changing file instead
of returning a mixed version. `abort_file_write(path, transfer_id)` explicitly
cleans an unfinished upload. Save a stable write request ID as the transfer ID.

Edge persists native intents in its existing request journal. Repeated IDs
with identical arguments attach/retrieve the original result; conflicting
arguments are rejected. Lost replies and failed commits remain UNKNOWN and
are never replayed. Stable IDs also cover upload chunks. No guest-side second
execution journal is introduced.

`session.stream(..., request_id=operation_id)` starts bounded background
collection at Edge. Subscribers read captured events using their returned byte
`cursor`; `session.events(operation_id, cursor=cursor)` reattaches without
resubmission. Detaching a subscriber does not cancel execution. The per-command
event spool is at most 8 MiB; overflow drops further output events while draining
the guest, and the final result sets `stream_truncated=true`. Final success is
exposed only after the journal commit. `session.query(operation_id)` reads that
same journal. `session.cancel(operation_id)` targets the exact active command,
and does not kill a later command. Cancellation resets this session.

MicroVM lifecycle admission uses the same durable registry: idle sessions can
be included in VM snapshots; active native calls/streams reject pause before
effects. An Edge restart with an interrupted native operation retires that VM
and preserves UNKNOWN, consistent with the existing interrupted-command
policy. Idle-session reattachment does not create a VM or shell. Edge graceful
shutdown waits for bounded stream collectors before releasing ownership.
Container memory snapshots remain unsupported. Old TB2 OpenEnv server images
do not acquire native features implicitly; use a prepared microVM/layered
container with the new agent.

Local tests execute the compiled agent with real shells and exercise SDK →
Edge → UDS, deduplication, commit faults, slow/detached subscribers and bounded
output. Real vsock, idle-session snapshot restoration, native layered-container
deployment and TB2.1/MBPP application regressions require the deferred Linux
acceptance; the UDS tests do not establish those gates.
