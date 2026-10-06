# Agent 环境接口

这个接口是训练框架与任务环境之间的边界，不代替 DSec 的沙箱 SDK。训练框架负责推理、采样、token/logprob 和训练轨迹；任务插件负责准备 instruction、选择环境与资源、解释动作、调用可信评分器；`DSecAgentEnvironment` 把两者接到持久 rollout worker。

发布范围：DSec 发布沙箱、调度、存储、恢复协议、统一环境接口与薄框架适配器。本文的 Miles CPU/GPU 内存排查、LoRA-only NVMe offload、decoder/NCCL 生命周期补丁、CE/输出投影分块与注意力探针，是固定训练镜像的接入实验；不计入 DSec 系统功能、性能优势或发布完成度。它们保留在实验目录，默认部署包和核心 wheel 均排除。

```text
Miles / verl / Uni-Agent agent loop
        │  policy messages, stable rollout/action IDs
        ▼
AgentEnvironment: reset → step* → evaluate → stop
        │                       ▲
        ├── TaskEnvironmentAdapter: prepare / step / evaluate
        ▼
ScheduledDSecClient → rollout worker → sandboxd → container or microVM
```

`AgentEnvironment` 协议和数据结构位于 [agent_environment.py](agent_environment.py)。调用方以稳定 `rollout_id` 创建一次 episode。`reset(policy_prefix)` 让任务插件准备 `EnvironmentSpec`，通过调度 client 创建或附着同一 rollout，并把任务 instruction 接在训练框架的消息前缀之后。返回的 `dialogue` 是 worker 保存的上下文。每个 `step` 携带稳定的 `step_id`、`action_id`、动作类型与载荷，以及原始 assistant 消息；返回动作结果和更新后的 worker 对话。`evaluate()` 只在插件拿到可信任务结果时返回有限分数；缺失、错误或无法验证的结果必须抛错，不得变成零分。`stop()` 释放本次沙箱。

训练框架在推理时使用 `dialogue["messages"]`，但不能用文本回填 token/logprob。模型的原始 assistant 消息应完整传给 `step`，以保持多轮上下文和 Miles 的 TITO 会话前缀一致。动作选择及回复格式解析仍由各框架的 agent loop 负责；此接口不规定 bash fence、轮数或采样参数。`EnvironmentAction.kind/payload` 允许 shell 以外的任务动作。TB2 插件 [tb2_dsec_environment.py](experiments/openenv_api/tb2_dsec_environment.py) 当前只接受 shell，并调用同一 VM 的官方 `tests/test.sh`；[通用计数插件](experiments/openenv_api/counter_dsec_environment.py)选择默认 microVM，使用 worker 的精确值校验。这两个插件共享同一环境接口，评分语义由各自插件负责。

底层 `ScheduledDSecClient` 的稳定 ID、持久执行记录、暂停恢复、结果未知时附着并对账，是本实现对 DSec 论文 `libdsec` 沙箱生命周期的扩展。论文给出的 SDK 用于选后端、创建沙箱、执行命令或工具调用和停止；它没有规定本文件的 agent 环境协议。当前 SDK 通过单机 Unix socket 访问 rollout worker，尚非论文的多机 apiserver/IAM 线协议。因此 API 形状和生命周期与论文相近，但不能声称二进制或网络协议兼容。

恢复规则：调用方必须保存并复用 rollout/action ID。收到 `ScheduledOutcomeUnknown` 时，使用同一 ID `attach` 或查询 worker dialogue，并对 `UNKNOWN` 状态执行 `reconcile`；不能新造 ID 重跑可能已生效的动作。训练框架重启后若丢失旧 token/logprob，沙箱可恢复完成，但该 episode 不得用于策略更新。`evaluate` 之前的 verifier 错误同样拒收该样本。

已验证：本地 28 项相关测试与 Linux 实验机 25 项接口测试通过，覆盖非 TB2 动作、TB2 插件、Miles 上下文传递、未知结果与 TITO 审计格式。2026-10-05 做了以下真实 VM 冒烟：

- 新建 `regex-log` episode：`reset → step(true) → evaluate → stop` 全程通过。rollout `55b7754a47f34dc8971a3e6b9cfb98e7`，VM `a2af7a966401`，官方 `tests/test.sh` 给出有效 0 分；保存了 CTRF、verifier 日志与 reward 证据。记录在实验机 `/home/xiaoxiaohu/dsec-reproduce/rl-rejoin-p0/agent-env-contract-20261005/fresh/result.json`。
- 用已有重连冒烟的两个训练端进程验证新 Miles 适配器：同一 VM 上的后续动作、对话恢复和官方判定通过；训练进程重启丢失旧 TITO 时返回 `reward_for_training=null`。rollout `0ec433e2b5f941d79cad51c562f64c55`，记录在同目录 `smoke3/result.json`。
- 通用计数任务（非 TB2）：独立的基础 microVM daemon 只接受本次 worker 的租约；经相同接口写入 `/rl-counter=3`，worker 精确值校验读回 `3`，得到有效 **1 分**。rollout `b3ed6c30949b4de9aa44e6e4b989bfcf`，VM `427041fd7d28`，记录在同目录 `counter-guarded4/result.json`。该插件把通用 VM 单命令超时固定在不超过 30 秒，超出会在提交前拒绝。
- Miles agent 循环通过显式 `dsec_environment=counter` 选择同一计数插件，使用确定性策略回调（无模型或 GPU）执行一步，得到有效 **1 分**。rollout `80fb38a599cc4faeb052ee49331b3b29`，VM `6ca5c4512399`，记录在同目录 `miles-counter/result.json`；worker 记录 `next_step=1`，最终 `STOPPED` 且无待处理动作。这证明 Miles→环境→reward 路径可以传递非 TB2 正分，不证明模型 TITO 或梯度更新。
- 真实 Qwen3.5-4B 经 Miles session server 对 `counter-example` 生成 3 次工具调用，取得可信 **1 分**；训练侧保存 72 个 response token、72 个有限 logprob、72 位 loss mask（有效位 39），TITO 会话前缀无不匹配。记录在实验机 `/home/xiaoxiaohu/dsec-reproduce/rl-rejoin-p0/mc-counter-model-r3/result.json`，原始逐轮 TITO 在同目录 `tito/542e3f2a10b44b5c97417ed884d3e14a/tito.json`。这是 rollout-only 验证，尚未产生参数更新。
- 较小的 Qwen2.5-0.5B-Instruct 在同一任务上也产生完整且可审计的 TITO，但实际执行的命令未写入正确值，得到可信 **0 分**。记录在实验机 `/home/xiaoxiaohu/dsec-reproduce/rl-rejoin-p0/mc-counter-small-r1/result.json`。该结果属于模型动作失败，不能把它当成接口或 verifier 故障。
- Qwen3.5-4B **thinking 模式**采用 `enable_thinking=true`、`temperature=0.6`、`top_p=0.95`、`top_k=20`、`min_p=0`、`presence_penalty=0`、`repetition_penalty=1`，在两次工具调用后主动回复 `TASK_COMPLETE`；可信奖励 **1 分**，295 个 response token 与有限 logprob 对齐，有效 loss mask 269 位，TITO 无不匹配。记录在实验机 `/home/xiaoxiaohu/dsec-reproduce/rl-rejoin-p0/mc-counter-qwen35-think-rollout-r1/result.json` 及同目录 `tito/c50e1db0fd4c4bf3a583a36c4cc182bc/tito.json`。

4B 全参数 FSDP 单步训练在初始化 actor 时达到节点 **30.06/31.18 GB**，被 Ray 内存保护终止；此时尚无 rollout、梯度或 checkpoint。原始日志在实验机 `/home/xiaoxiaohu/dsec-reproduce/rl-rejoin-p0/mc-counter-qwen35-train-r1/train.log`。Miles 当前 FSDP 后端只创建 AdamW 优化器，而 LoRA 注入只支持 Megatron 后端；当时的宿主机虚拟环境未安装 Megatron（后续官方容器已包含）。因此这台 31 GiB 主机无法凭现有训练配置完成 4B 全参数更新。[单步脚本](experiments/openenv_api/run_miles_counter_one_step.sh)现在对该组合设置 64 GiB 主机内存安全门槛，在启动 Ray 前明确拒绝容量不足的机器；64 GiB 是预留参数、梯度、AdamW 状态和推理服务的保守验收条件，并非测得的最小可运行值。

随后使用固定 digest 的官方 `radixark/miles:v0.1.0` 镜像（内含 Megatron、Megatron-Bridge、Transformer Engine 和 SGLang）做单卡 Megatron LoRA 探针。过宽的 `attn` 目标导致 SGLang 尝试给 `RadixLinearAttention` 包装 LoRA 而失败；改为 MLP 目标后，训练模型初始化曾出现 CUDA OOM。进一步追踪分配记录确认：Megatron-Bridge 的 `Qwen3VLGPTModel` 在父类创建 decoder 后又构建替代 decoder，旧对象直到新对象创建完成才被替换，失败时替代 decoder 已额外占约 **4.235 GiB**。在重建前加入 `del self.decoder` 后初始化通过，构建完成时 allocated 约 **8.77 GiB**；此前“16 GiB 无法完成该模型初始化”的结论已被此验证更新，不能据此断言必须更换 GPU。补丁、环境、证据口径和待提交上游 PR 的验证事项见 [decoder 初始化改动记录](RL_FRAMEWORK_ADAPTERS.md#待评估上游-prqwen-decoder-初始化显存峰值2026-10-05)。

随后配合磁盘 offload、decoder MLP-only LoRA、`qkv-format=bshd` 和 `attention-backend=unfused`，Qwen3.5-4B thinking 探针取得可信 **1 分**与完整 TITO，完成前向、反向，记录有限非零 `grad_norm=2.4586777742111514`、`valid_step=true` 并保存 adapter。结果位于实验机 `/home/xiaoxiaohu/dsec-reproduce/rl-rejoin-p0/miles-official-v010-counter-lora-unfused-r1/`。该次运行之后 SGLang 恢复 KV 内存时 OOM，未完成完整循环；该历史失败的后续定位与修复如下。运行入口为 [受控探针脚本](experiments/openenv_api/run_official_miles_counter_lora.sh)。

进一步原生分配追踪发现：Miles 的单 rank NCCL 组被排除在可卸载通信组注册表之外，训练后新增的 4 × 512 MiB NCCL 缓冲区没有随 `sleep()` 释放。将这些组纳入销毁/重建后，`miles-singleton-fix-cycle2-r1` 保持 SGLang 显存比例 0.8、无 logprob 分块，完整两轮推理→训练→恢复推理通过，exit=0。两次 reward=1、TITO 对齐、optimizer step=1/2，两个 checkpoint 的 192 个 adapter 张量均发生变化；两轮卸载后的整卡占用稳定约 1.66 GiB，修复前约 4.16 GiB。详情与复现配置见 [Miles 显存生命周期定位记录](RL_FRAMEWORK_ADAPTERS.md#原配额两轮完整验证通过)。这仍是短计数任务验证，不代表 TB2.1 全任务或长上下文训练通过。

上述 rollout 最终均为 `STOPPED`，`pending=None`，隔离 worker 已退出；计数任务的隔离 daemon 也已退出。以上证明至少两个任务插件可共用环境协议。Miles 的[任务插件注册表](experiments/openenv_api/dsec_task_registry.py)通过可信样本元数据选择插件；生成/reward 护栏同时核对插件和评分器，默认仍为 TB2。正式 `dsec-elastic-rollout-worker` 已于 2026-10-05 升级至含持久对话接口的完整运行时，正式入口及 worker 重启验收见下文。正式 sandboxd 的创建门禁仍只认可正式 worker 的调度租约；隔离 worker 不能绕过门禁。TB2.1 最初保留独立 netns daemon 路径；2026-10-05 已注册到正式通用 daemon，见下文。verl 和 Uni-Agent 仍需要各自的薄 agent-loop 适配器及实际训练验证。


## 正式接口部署与验收（2026-10-05）

正式 `sandboxd` 和 rollout worker 已作为完整 95 个顶层 Python 文件升级，14 项源码差异在部署前核对；旧服务在空闲时切换，114 个旧 STOPPED 记录正常加载。备份位于实验机 `/home/xiaoxiaohu/dsec-reproduce/service-code-pre-agent-20261005-162217`。

- 正式入口的计数任务 `c9791af60d0b4f289d1f233de392fb56`：在 VM `a0aade376017` 执行一次非幂等加数命令；新客户端附着原 VM、恢复完整 dialogue，重复 action 返回原结果，计数仍为 3，可信 reward=1。
- 正式入口的 Miles agent 回调任务 `37f8b7a0bb2b4c8d9b1a4e2ac6bee4ae`：经同一插件注册表与 worker 取得 reward=1。此项使用确定性策略回调，没有启动模型、GPU 或训练。
- 正式 worker 受控重启任务 `84df816c626c477ba65e0ce720e5267c`：worker PID 从 3831974 变为 3839683，原 VM `e1c48acf88fb` 与完整 dialogue 保持；重复已完成 action 未再次执行，reward=1。此项覆盖步骤完成后的计划性 worker 重启，未覆盖执行中崩溃。
- 完整发行包的真实 TB2.1 `regex-log` 任务 `9b668b8e7cbe434ca77ce0e94685f7fc`：系统 `/usr/bin/python3` 经同版 worker 和既有独立 netns daemon 完成创建、动作、客户端重连、重复动作、官方 `tests/test.sh` 评分和清理。该次动作只有 `true`，官方 reward=0 属于有效未解题结果；CTRF 1768 B、verifier 日志 1558 B、reward 文件 2 B 已保存并记录摘要。该路径未经过正式通用 worker 的 socket。

以上最终状态均为 STOPPED、pending=null、lease_held=false。证据在实验机 `elastic-next-agent/{formal-counter,formal-miles-counter,formal-worker-restart,release-tb21}/result.json`，本机汇总副本在 `.runtime/agent-production-20261005/`。

核心服务、统一 AgentEnvironment 协议和上述两个任务插件不以 OpenEnv 为必要依赖。TB2 评分的两个命令辅助函数已改由 [tb2_verifier_command.py](experiments/openenv_api/tb2_verifier_command.py)提供；官方任务 `tests/test.sh`、CTRF 完整性检查、有限二元 reward 和持久证据导出保持不变。早期 OpenEnv 对照脚本仍保留可选依赖。首轮暂存回归暴露的缺模块、缺制品清单和工具盘摘要不匹配均被拒收，未折算为零奖励；当前验收显式指定实际部署工具盘清单。

[完整打包工具](tools/package_agent_runtime.py)默认选择 `runtime`，包含核心、任务插件、薄 Miles 适配器、评分制品清单、网络测试辅助源码和验收脚本，另记录发布类型与逐文件 SHA-256；不包含训练内存补丁、GPU 探针、训练启动脚本、任务数据、镜像或模型。[部署脚本](experiments/3fs-single/deploy_agent_environment_runtime.sh)先做差异、Linux 回归、空闲和依赖检查，再备份/切换/校验，失败自动恢复。核心 wheel 共 35 个运行时模块，已补入 egress_proxy；在 Linux 的 `/tmp` 中只通过 wheel 导入 sandboxd、worker、AgentEnvironment 和代理模块通过。Mac 与 Linux 的 26 项相关单元回归均通过。

生成部署包：

```bash
python3 tools/package_agent_runtime.py /tmp/dsec-agent-release.tar.gz
```

仅复现固定 Miles 镜像接入实验时，显式生成独立实验包：

```bash
python3 tools/package_agent_runtime.py /tmp/dsec-miles-training-experiment.tar.gz \
  --profile training-experiment
```

实验包是运行时加实验文件的集合，供既有训练脚本使用，不作为 DSec 默认发行。此前 `release-2b-*`、`release-4b-*` 和 `release-projection-*` 均属于训练实验制品。

独立 TB2.1 验收需显式提供环境 socket、任务目录和当前工具盘清单。源码路径仅使用包内的 `code` 与 `adapters`，不包含 OpenEnv：

```bash
base=/home/xiaoxiaohu/dsec-reproduce
release=$base/elastic-next-agent/release-check
PYTHONPATH="$release/code:$release/adapters" /usr/bin/python3 -B \
  "$release/adapters/smoke_agent_environment_contract.py" \
  --out "$base/elastic-next-agent/release-tb21-next" \
  --worker-script "$release/code/rollout_workerd.py" \
  --sandbox-socket "$base/openenv-api/overlaybd-live/service.sock" \
  --tasks-dir "$base/openenv-api/tb2-suite-2-1/tasks" \
  --canonical-verifier-manifest "$base/openenv-api/verifier-artifacts/uvx-0.9.5-runtime-r1.ext4.manifest.json" \
  --adapter tb2 --task-id regex-log
```

真实 Qwen3.5-4B 两轮训练随后也已切到正式 worker 并通过，见下节；此前确定性策略回调仍只是接口验收。短计数训练不能表述为 TB2.1 长上下文训练通过。


## 真实 Miles 训练接到正式入口（2026-10-05）

`miles-formal-worker-cycle2-r2` 通过正式 `elastic-worker/worker.sock` 与 `live/service.sock` 完成 Qwen3.5-4B 两轮 thinking Megatron LoRA 训练。未创建隔离 worker/daemon；客户端导入正式 `service-code` 的 SDK 与 AgentEnvironment。固定官方 Miles 镜像、decoder 初始化修复、单 rank NCCL 组卸载修复、磁盘训练 offload、SGLang 0.8 配额、MLP-only LoRA、bshd 和 unfused 均与此前通过的配置保持一致。thinking 采样仍为 temperature=0.6、top_p=0.95、top_k=20、min_p=0、presence_penalty=0、repetition_penalty=1。短任务 max sequence 2048、response 1024、最多 3 次工具回合；没有以此覆盖 TB2.1 的 32768 response 配置。

- exit=0，训练进程墙钟 **248.946 秒**，不含外层容器清理和验收工具时间。
- 两次可信 reward=1，response/logprob/mask 长度分别 295/295/295 和 174/174/174，有效 mask 位数 269、160，TITO 均无不匹配。工具调用数 2、1，均以 TASK_COMPLETE 结束。
- optimizer step=1/2，grad_norm=2.4581242057513855 / 4.135192301290515，均有限非零。两个 checkpoint 间 192 个 adapter 张量发生变化，包括 96 个 LoRA B，最大步间差异 4.991888999938965e-7。此项验证实际参数更新，未测学习收益。
- rollout `090d0b8826ef41539a3546815660cf1c` / VM `5c95b31b052d`，rollout `6f959101ebab4e4baaaf5a9d2fd6be18` / VM `e831b448b5d0`，最终均 STOPPED、pending=null、lease_held=false。
- GPU 定时采样峰值 14110 MiB，训练容器 memory.peak 约 26.62 GiB；后者不包含 DSec VM/服务的全部成本。结束后整卡约 35 MiB，可用磁盘约 70 GiB，主机 MemAvailable 约 28.1 GiB，正式服务与两项 3FS 依赖健康；121 条 VM 记录全为 STOPPED。训练磁盘 offload 临时文件已删除，checkpoint、模型回复、TITO 和 worker 记录保留。

第一轮尝试 `miles-formal-worker-cycle2-r1` 在第一步取得 reward=1、完成训练并恢复推理后，第二个 episode 被正式 scheduler 的主机内存保留规则阻塞。当时 MemAvailable 约 3919 MiB，而准入要求减去 512 MiB 任务声明后仍至少 4096 MiB；等待原因为 memory_pressure，没有 OOM。停止本次训练后主机 MemAvailable 回到约 28485 MiB，GPU 35 MiB。只停止了本次作业明确拥有的两个 rollout，保存了 scheduler-blocked.json、interruption.json、部分模型/训练证据；没有将中断样本记成零奖励。

随后在正式服务空闲时，把 `budget.json` 中 `min_memory_free_mb` 从 4096 改为 2048，其余任务 CPU/内存/磁盘预算保持原值。备份为 `elastic-worker/budget-pre-formal-training-20261005.json`；worker 重启加载新值并检查队列及依赖，失败路径恢复备份。重跑两轮没有调度阻塞。该改动是这台训练与 VM 共机的资源保留配置调整，没有降低模型主存占用，也没有削减 SGLang 的 GPU 配额；不能据此推广多任务并发上限。

[训练入口](experiments/openenv_api/run_official_miles_counter_lora.sh)现支持外部正式服务 socket；[模型适配器](experiments/openenv_api/miles_dsec_agent_function.py)在创建前持久保存作业 episode 归属，父训练 runner 退出时仅清理该作业的 ID。[验收工具](experiments/openenv_api/audit_miles_dsec_cycles.py)核对训练轨迹、归属 ID、worker 的奖励/对话/清理状态、有限梯度及实际参数变化。14 项 Miles 适配器/奖励护栏测试通过，包含创建失败前归属已保存的回归。训练生成的新文件已归还实验用户读取。

运行配置：

```bash
base=/home/xiaoxiaohu/dsec-reproduce
DSEC_AGENT_CODE_DIR="$base/rl-rejoin-p0/formal-agent-code-20261005" \
DSEC_EXTERNAL_WORKER_SOCKET="$base/elastic-worker/worker.sock" \
DSEC_EXTERNAL_SANDBOX_SOCKET="$base/live/service.sock" \
DSEC_FIX_DOUBLE_DECODER=1 DSEC_TRAIN_DISK_OFFLOAD=1 \
DSEC_FIX_SINGLETON_NCCL_OFFLOAD=1 DSEC_SGLANG_MEM_FRACTION=0.8 \
DSEC_NUM_ROLLOUT=2 bash "$base/rl-rejoin-p0/formal-agent-code-20261005/run_official_miles_counter_lora.sh" \
  "$base/rl-rejoin-p0/miles-formal-worker-cycle2-r2"
```

完整证据在实验机 `rl-rejoin-p0/miles-formal-worker-cycle2-r2/{result.json,cycle-audit.json,episode-cleanup.json,source-observed.json,worker-records,tito,checkpoint}`；本机主要 JSON 副本在 `.runtime/miles-formal-worker-cycle2-r2/`。重跑应使用新输出目录，避免覆盖既有证据。TB2.1 官方任务随后已注册到同一正式入口；真实模型训练验收状态见下文。

当前固定 SGLang 的 `TorchMemorySaverAdapter.region(..., enable_cpu_backup=False)` 默认关闭 CPU 备份；KV 内存池以 `kv_cache` 区域按该默认值分配。`release_memory_occupation` 对 KV 执行 pause 并 flush_cache，恢复时 resume 重新提供 GPU 空间；底层仅在 enable_cpu_backup 为真时分配主机备份并做 DeviceToHost。因此当前 KV 内容没有在训练阶段卸载到主存保存。后续已定位主要主存占用为 Miles 的完整 actor pinned CPU 备份，并完成 LoRA-only/NVMe 优化验证，详见 [主存报告](MILES_HOST_MEMORY_REPORT.md)。

## TB2.1 注册到正式入口（2026-10-05）

已有 89 个 TB2.1 环境已通过 [注册器](experiments/openenv_api/register_tb21_environments.py) 纳入 `elastic-environments/microvm-catalog.json`，与原来三个通用环境共用正式 worker 与 daemon。固定 `terminal-bench-2.1` 源码提交 `7131e4375048a0e408a8fb404b5f499d726b695b`，验证任务索引、已转换根盘的全字节等价记录、根盘 lower 和 EROFS 制品摘要；没有复制或重建磁盘。保留已有压缩层、离线合并层与 `install-windows-3.11` 的 DAX 选择。OverlayBD 根盘已完整承载启动文件系统，目录可以省去不参与运行的源 ext4 模板；这避免对 89 份逻辑长度各 10 GiB 的稀疏导出反复计算摘要，实际 lower 仍完整校验。

[部署工具](experiments/3fs-single/deploy_tb21_formal_entry.py)发布完整发行包，检查正式队列空闲，备份源码、目录、预算及 systemd 配置，失败时回滚。正式 network helper 限定 `live` 目录，使用与旧 TB2 服务不同的 veth 地址范围，沿用每 VM 独立 netns 和私网隔离规则；当前 daemon 并发容量保持 4。任务插件读取正式目录中的 CPU/内存配置作为默认资源申请，明确传入的估计仍可覆盖。调度内存池从 4096 调到 8192 MiB，以覆盖全部任务的最大声明；host 保留 2048 MiB、磁盘保留 52000 MiB 均保持原值，这没有实际预分配 8 GiB RAM。

`openssl-selfsigned-cert` 的正式 SDK 验收通过：创建约 1.67 秒、重新附着同一 VM、复用已提交动作结果、同 VM 官方 `tests/test.sh`、CTRF/日志/reward 落盘以及停止后释放租约。空操作得到有效的 0 分，不算模型解题结果。37 项 Linux 回归全部通过。

实验机工作目录：`rl-rejoin-p0/tb21-formal-entry-20261005`。首次部署备份为 `backup-20261005T103012Z`。原有 TB2 专用服务未删除；新训练入口只使用 `elastic-worker/worker.sock` 与 `live/service.sock`。

真实 4B 模型在正式入口的 `openssl-selfsigned-cert` 已取得官方 6/6、reward=1（`miles-openssl-cycle2-r2`，rollout `c8891a1f72da423ab7132c1d12ae87af`）。这证明一个真实任务可经新入口完成，不代表 89 题全部执行通过。4B 两轮训练未通过：先后定位到整份 logits 的 FP32 转换、整份温度缩放副本及训练 CE 保存 FP32 softmax 张量。`r5` 的 4227-token 样本通过 rollout 与 log-prob 计算，但训练 loss 仍 OOM；其 3802 行 response × 248320 词表的 FP32 张量约 3.52 GiB。真实 fused CE 的保存张量探针证实普通分块仍保留全部 FP32 块；同分块布局的重计算可避免这项保存，探针的 logprob 和 logits 梯度位级一致。这是独立诊断，尚未用于完整 4B 训练。

固定版本保留 BF16 logits、在分块 FP32 内缩放温度的路径已完成真实 Megatron wrapper 数值检查：温度 1.0/0.6 的小矩阵 logprob 与模型梯度位级一致，entropy 最大差约 4.77e-7。更大矩阵的整量/分块归约存在小幅浮点差异，不能将小矩阵结果泛化为所有形状位级一致。训练 entropy coefficient 保持 0。

按用户决定，当前切换 `Qwen/Qwen3.5-2B` 完成同任务两轮 RL 验收；HF revision `15852e8c16360a2fea060d615a32b45270f8a8fc`，权重 SHA-256 `aa33250c4fc64891ddfaba3a314fd9542ea371843c387178b425fbcc5ed680b1`。固定官方 Miles 镜像没有 2B 参数脚本，[适配参数](experiments/openenv_api/qwen35_2b_model_args.py)复用其 Qwen3.5 spec，按官方配置声明 24 层、hidden 2048、FFN 6144、8 heads/2 KV heads，启动前校验实际 HF config。thinking、32768 response、65536 context、16 工具回合、temperature=0.6/top_p=0.95/top_k=20/min_p=0/presence_penalty=0/repetition_penalty=1 均保持。模型能力结果与原 4B 分开记录。正式 worker 已在队列空闲时开启 verifier 联网兜底和日志保存；制品路径仍优先复用，联网使用现有代理。

2B 启动命令（使用新输出目录）：

```bash
DSEC_AGENT_CODE_DIR=/home/xiaoxiaohu/dsec-reproduce/rl-rejoin-p0/tb21-formal-entry-20261005/release-2b-r5/adapters \
DSEC_MODEL_PATH=/home/xiaoxiaohu/models/Qwen3.5-2B \
DSEC_MEGATRON_MODEL_NAME=qwen3.5-2B \
bash /home/xiaoxiaohu/dsec-reproduce/rl-rejoin-p0/tb21-formal-entry-20261005/release-2b-r5/adapters/run_official_miles_tb21_lora.sh \
  /home/xiaoxiaohu/dsec-reproduce/rl-rejoin-p0/tb21-formal-entry-20261005/miles-openssl-2b-cycle2-r3
```

修复前 2B 试验 `miles-openssl-2b-cycle2-r1` 已完成第一轮训练与推理恢复（3287-token 轨迹、官方 0 分、零梯度），并产生一个官方 1 分的完整任务轨迹。但 agent 将最后的完成说明归为 `invalid_format` 并返回 None，导致 Miles 排除这个正分后继续重试；因此没有完成第二轮训练。已受控停止该任务，四个 job-owned 沙箱全部 STOPPED、pending=None、lease=false。最初 32768-token 截断回复在约 2045 字符处已结束思考段，后续主要为重复代码块；它仍因截断被排除，不能作为纯思考耗时样本。

已修复完整非命令终止回复的判定：保留真实 verifier 0/1 分，标记 `end_reason=invalid_format`、`format_valid=false`，不执行无法解析的文本。截断、模糊 thinking 边界、重连后不完整轨迹与硬 TITO 不一致仍被拒绝。新增回归验证正分和零分均不会被终止格式覆盖；本地/Linux 各 16 项通过。格式修复后的 `miles-openssl-2b-cycle2-r2` 产生完整 5029-token、官方 0 分的样本，但仍在第一轮训练 CE 的 FP32 chunk 分配时 OOM；两个 job-owned 沙箱均已 STOPPED、pending=null、lease=false，尚无 checkpoint。

`release-2b-r5` 已将前述重计算接入真实 policy loss 路径，只针对当前单 rank、普通分块 CE；其他模式明确拒绝。仅在需要梯度时以相同 FP32 cast、温度缩放和 fused CE 重计算，推理 logprob 前向不变。集成后的固定镜像 CUDA 探针逐位验证相同布局的 logprob/梯度，并确认 FP32 保存量从 67,239,936 B 降为 0；已有 BF16 logits 不额外复制。`miles-openssl-2b-cycle2-r3` 已完成两轮（exit=0，812.412 秒），正式 worker RPC 审计通过；两份有效轨迹 response token 为 5044/5771，完整 TITO、有限 logprob、loss mask 和两份有限 checkpoint 对齐，两个 optimizer step 及每次恢复推理均完成。两题官方分数均为 0，梯度均为 0、adapter 未变化，不能宣称有效学习或任务解题通过。三个 job-owned episode 全部 STOPPED、pending=null、lease=false；一份 32768-token 截断回复被排除。GPU 采样峰值 14862 MiB、容器 memory peak 16.211 GiB 是整轮资源口径，不是训练单阶段峰值。审计证据为同目录 `cycle-audit.json`，本机副本 `.runtime/tb21-formal-entry-20261005/2b-recompute-cycle-audit.json`。

用户确认修复通过后切回 4B。`miles-openssl-4b-recompute-cycle2-r1` 使用同一 `release-2b-r6`（相对 r5 只修正审计器对零参数变化的 max 空集处理）重新验收 Qwen3.5-4B；4139-token 有效 0 分轨迹通过，但在训练输出层 BF16 logits matmul 的 2.13 GiB 分配处 OOM，尚未执行 optimizer step。此时 PyTorch allocated=9.55 GiB、reserved-unused=1.80 GiB、驱动 free=1.75 GiB；不能将碎片猜测当成已证实唯一根因。owned episode 已停止并释放。`release-4b-r7`/`miles-openssl-4b-recompute-cycle2-r2` 仅增加 `PYTORCH_ALLOC_CONF=expandable_segments:True` 做了一次分配器验证；119.827 秒后 exit=1，在模型构建/恢复的 90 MiB 分配处失败，当时 SGLang 占 12.69 GiB，训练进程占 2.77 GiB，尚无 rollout/checkpoint。该配置未解决问题，具体对内存卸载交替的影响尚未定因；不能断言只是原始碎片问题，已撤回 launcher 开关。稳定发行仍为 `release-2b-r6`（重计算路径保留）。因此目前可接受的是 2B 两轮链路验证，4B 完整训练仍未通过。所有训练容器已退出并清理 GPU，未将分配器实验部署为正式默认。进一步 4B 优化应针对输出投影及其梯度的整矩阵占用，不能靠降低 SGLang KV 预留掩盖训练侧问题。 2B actor 基础 allocated 约 4.28 GiB，4B 约 8.79 GiB；两者词表同为 248320，因此相同 token 数的输出矩阵不会随模型参数减半。4B r1 失败时 SGLang 仅占约 586 MiB，大部分缓存已卸载，不能将其归为 KV 预留不足。当前训练与推理进程均已退出，正式 worker 仍健康，磁盘余量约 70.31 GiB。用户还允许在 KV 有余量时降低 SGLang 启动预留：历史 4B/0.8 的 KV 为 59,672 token、K/V 合计约 1.82 GiB，Mamba 状态池约 1.68 GiB；当前 2B/0.8 的 KV 为 359,982 token、K/V 合计约 4.12 GiB，Mamba 状态池约 3.72 GiB。必须按模型重新测量，不能按 2B 余量推定 4B 可降多少。当前根因验证保持原配额。

用户提出用 presence penalty 减少重复输出。Qwen 官方允许 0–2，并特别提醒 2B thinking 容易陷入循环。当前 Miles sampling-support replay 的 validator 明确拒绝非零 presence/frequency penalty，因为训练侧无法复现其 logits 变换；这不代表 SGLang 推理不支持。正式 RL 维持 presence_penalty=0，不绕过校验。presence_penalty=1.5 的单独推理对照尚未执行，若用于 RL，需另行实现并验证训练概率回放。

训练结果审计可显式选择 `--allow-zero-rewards`：仅接受官方有效的 0/1 分，并核对样本与持久 worker 记录的评分一致、TITO/loss mask 完整、两份有限 checkpoint、训练步骤及资源释放。有效 0 分不算解题成功；零梯度/无 adapter 变化也不会报告为有效学习更新。原短计数任务的正分、非零梯度与参数变化门槛仍保持默认。

权限生产化尚未完成：当前实验账号仍兼有 worker 与 Docker 权限。目标是安装阶段由管理员一次性创建独立服务身份、受控网络/块设备 broker 和服务配置；训练用户只访问 DSec API，按任务启动无需新增 sudo 授权。多租户鉴权、独立 worker 身份与 Docker 权限隔离仍是后续工作，当前 helper 安装不能替代这些边界。

### 生产部署的权限收敛计划

第一步是提供幂等的节点安装、升级与卸载入口。管理员在安装阶段配置 KVM/ublk、独立服务账号、状态目录、网络及系统级 systemd 服务；特权服务的可执行文件和配置由 root 持有，训练用户不能修改。安装先检查依赖并输出具体缺项，升级包含备份与回滚。日常创建、执行、暂停、恢复和清理走 DSec API，不要求训练用户拥有 sudo、Docker socket 或宿主设备权限。

第二步将训练客户端、调度 worker、VMM 服务与特权资源服务分开运行。worker 不属于 docker/kvm 组，不直接操作 netns、ublk 或 Docker；VMM 由受控启动路径以非 root 身份运行。网络和块设备服务只接受带资源所有权校验的结构化请求，限定设备、目录及资源标识。Docker 代理目前仅限制命令族，仍不足以约束任意 `docker run` 参数；生产接口必须约束镜像、挂载、设备和启动选项，拒绝 privileged、宿主命名空间及任意宿主路径。制品发布和运行时写入使用不同权限，避免普通用户可写的文件成为特权服务的配置或执行入口。

第三步作为部署验收：使用没有 sudo、Docker 和 KVM 权限的独立训练账号完成真实任务及服务重启后的恢复；验证越权操作、跨任务访问和任意宿主挂载被拒绝，任务停止后资源能回收。单机阶段先用 Unix socket 身份与目录权限建立边界；跨机器开放控制面前，再补服务认证、租户授权和密钥管理。当前已有受控网络 helper 与 ublk 系统服务，但尚未通过上述完整身份隔离验收，不能据此宣称生产安全边界已完成。


### 分块输出投影验证（2026-10-05）

4B 输出层整份 BF16 logits 的 OOM 不能由 CE 后处理分块解决。[分块投影适配器](experiments/openenv_api/miles_chunked_projection.py)将 frozen LM head 的投影、原 fused CE 和温度缩放放在同一个 token chunk 内，只向 Megatron 后续传递逐 token 的 logprob/entropy 两通道；训练通过 checkpoint 重算同一块，避免保存完整词表 logits 及其整矩阵梯度。输出 head 在 forward 作用域内替换并在 finally 恢复，无常驻权重引用，不修改词表、采样、RL loss 或 SGLang。当前范围明确为 TP/CP/PP=1、BSHD、batch=1、冻结无 bias LM head、无 MTP/true-on-policy/sequence-parallel；其他组合拒绝。仅在 `training-experiment` 包内通过 `DSEC_CHUNKED_OUTPUT_PROJECTION=1` 显式启用，不进入 DSec 默认发行。

固定镜像 CUDA 探针 `projection-probe-r9.log` 对照实际 Megatron frozen linear，覆盖 1024/248320 词表、513 token、temperature=0.6、entropy coefficient=0/0.01、原 response 偏移及非零 LoRA 梯度。logprob/entropy/loss 相同，BF16 LoRA 梯度最大差 6.1035e-5（分块累加数值差异，不能宣称梯度逐位一致）；完整词表路径保存中间张量 509,631,129 B → 149,424 B，探针额外 allocated 峰值 973,141,504 B → 222,711,808 B。该峰值是探针口径，不代表实际 4B 训练。Linux 16 项 agent/生成护栏回归通过。

`release-projection-r9` 的真实启动 `miles-openssl-4b-projection-cycle2-r1` 因生成补丁将普通 import 放在 future import 前而在启动阶段失败，没有 rollout/checkpoint。`release-projection-r10` 修正严格锚点并在生成时 compile 所有补丁，固定镜像实际 model import 通过。`miles-openssl-4b-projection-cycle2-r2` 已取得真实官方 1 分、完整 4251-token TITO，但在训练阶段因外层 Qwen3VLModel 的 output_layer 位于 language_model 而定位失败，尚未进入投影。`release-projection-r11` 增加沿 module/language_model 查找的明确包装路径，无全局参数引用；GPU 探针新增 DDP→多模态 language_model 包装及 forward 恢复检查，数值与保存量结果保持。`miles-openssl-4b-projection-cycle2-r3` 使用 r11、原 4B、thinking、32768 回复上限、65536 context 和 SGLang 0.8，取得官方 1 分及完整 5458-token 轨迹；随后在输出层之前的 unfused attention `torch.bmm` 处 OOM，未执行 optimizer step，两轮训练未通过。该结论属于 Miles 训练实验，不是 DSec 沙箱故障。之前 r8 探针发现 CE 返回 `[N,1]` 而 entropy 为 `[N]` 的形状不匹配，在 r9 将单列 logprob squeeze 后修复；r8 未用于真实训练。


### 真实 TB2.1 的 4B 正分训练验收通过（训练实验，2026-10-05）

`miles-openssl-4b-flash2-cycle2-r1` 使用独立 `training-experiment` 制品，通过正式 DSec SDK、worker 与 microVM 完成 `openssl-selfsigned-cert` 两轮训练。固定官方 Miles 镜像保持，使用 FA2 2.7.4.post1，输出投影分块与 CE 重计算，Qwen3.5-4B thinking、32768 回复上限及原采样保持。严格审计没有放宽奖励或梯度条件：两次官方 reward=1，response/logprob/mask 分别 4657/4657/4657 与 2729/2729/2729，有效 mask 4173/1825，TITO 无不匹配；两步梯度范数 0.1018514100/0.8552961829，均有限非零。两 checkpoint 间 192 个 adapter 张量变化，96 个 LoRA B 变化，最大差 4.991889e-7；exit=0，训练进程墙钟 441.612 秒。第二步日志中的 lr=0 是完成线性调度后的读数，实际参数差异已核验。

第一轮以 max_turns 结束，第二轮以 invalid_format 完成说明结束；均未截断且官方评分、完整轨迹有效，符合已有终止格式护栏，不把无法解析的文本执行为命令。两个 episode 没有重试排除，最终 STOPPED、pending=null、lease=false，CTRF、verifier 日志、原始模型回复、TITO、worker 记录与 checkpoint 保留。第一步训练后的第二次真实 rollout 验证推理恢复；第二步后权重恢复、LoRA 同步和继续生成接口成功，训练随即正常退出，没有第三次任务生成。

严格审计 `cycle-audit.json` 位于实验机同一 job 目录，本机副本 `.runtime/tb21-formal-entry-20261005/4b-flash2-cycle-audit.json`。GPU 回到 35 MiB，无训练容器、活跃/等待租约；临时 NVMe offload 删除，剩余磁盘约 70.07 GiB。训练峰值口径及 FlashAttention 绕过历史见 [训练侧验证记录](RL_FRAMEWORK_ADAPTERS.md#flashattention-临时绕过的纠正训练实验2026-10-05)。本项验证真实正分梯度更新闭环，不等于 89 个任务全部通过、完整 32768-token 训练通过或模型学习收益已证明。训练侧内存补丁仍不进入 DSec 默认发布。


## 从准备态基线创建独立 episode（2026-10-05）

`ScheduledSandbox.seal_baseline(allow_prepared_state=True)` 将可信初始化完成的 VM 封存为只读准备态基线。仅允许 microVM、完整内存快照、独立 netns（或无网络），并且必须在 policy dialogue 和 verifier 开始之前调用。guest 需要 Python 3 标准库；保存前检查 TCP 连接，拒绝仍在进行中的连接。封存之后执行、恢复、再次初始化 policy dialogue 等操作均被拒绝，显式 stop 和 TTL 仍然有效。

新 episode 通过 `ScheduledDSecClient.create(..., baseline_rollout_id=source.id)` 创建，也可使用 `DSecAgentEnvironment(..., baseline_rollout_id=source.id).reset(...)`。调度器照常授予每个分支资源 lease；源基线目前保守保留原 lease 和容量槽，直到显式删除。基线必须与新 episode 的 task 和 profile 相同。分支有独立 sandbox/rollout ID、netns、vsock、OverlayBD upper 和新的轨迹；不会继承 preparation 的命令历史、policy context 或 reward。与已有 libdsec 风格 create/pause/resume 的调用兼容，没有 baseline 参数时保持现有创建路径。

只读 EROFS 层继续复用。OverlayBD 的准备态增量层在封存时复制一次，校验后发布到本地 `.fork-layers/objects/<sha256>.commit`；各分支持有同一只读 lower 的持久引用，并创建自己的 writable upper，不再逐分支复制准备层。最后一个持有者停止后回收共享对象；启动恢复保守保留非 STOPPED 和异常注册表记录的引用。内存从同一个快照以 Firecracker File backend 的 MAP_PRIVATE 映射恢复，这项内存 COW 在原实现中已经存在。源磁盘路径由一个固定恢复用设备提供，仅在暂停的 snapshot load 阶段使用；随后在 vCPU 恢复之前 PATCH 为分支磁盘，vsock 和 TAP 也映射到新分支。恢复后注入新 host 熵、执行 kernel CRNG reseed，并写入分支 machine-id 和 `/run/dsec-episode-id`。持久注册表记录来源 ID 和 generation，后续分支 pause/resume 使用自己的快照。

这是一项显式选择的准备态分叉扩展，不默认复制任意正在运行的 agent。构建阶段必须移除答案、凭据和外部连接；应用自身已初始化的 PRNG 或外部服务会话需要 adapter 自己处理。这里只重置 guest kernel RNG，不声称重新初始化任意进程内部随机状态。

论文边界：DSec 第 6.1 节明确描述 `pack_diff`（增量磁盘检查点恢复成新沙箱），第 6.3 节描述同一 microVM 的内存快照暂停恢复。这里把准备态磁盘和运行内存一起用于新 episode，是我们系统的扩展接口，不能称为论文公开的原样 API。[DSec 第 6 节](https://arxiv.org/html/2609.22978#S6)

验证入口为 `experiments/3fs-single/verify_episode_fork.py`，只使用 SDK/正式 worker 和现有 TB2.1 adapter，不依赖 Miles、模型或 OpenEnv 服务。启动耗时需区分 snapshot 完整性校验、磁盘/netns 准备、VMM 恢复和分支初始化；本特性不承诺 60 ms。


本次验收通过（`episode-fork-validation-r5/result.json`）：一个 `openssl-selfsigned-cert` 准备态基线生成两轮、每轮两个独立 episode。四个分支的 guest-agent 内存 token 和计数位置与基线一致；基线中预启动的 HTTP 服务内存值分别在各分支独立变化；同路径写入互不影响。统一 `DSecAgentEnvironment.reset/step/evaluate/stop` 入口保留新 episode 的独立 policy dialogue。第一轮一个分支生成证书后，官方 TB2.1 `tests/test.sh` 得 1 分，兄弟分支仍无证书；第二轮也无前轮答案。基线 memory/state/OverlayBD config 的 SHA 在使用期间未变化，删除基线后已有分支继续执行，并能从自身快照暂停恢复。全部五个 job-owned rollout 停止、租约释放、调度 active/pending 清零。本地相关回归 34 项（1 项平台跳过），部署时 Linux 回归 27 项通过。

首版历史性能（r4，已由 COW 版本替代）：冷创建 1.551 s，基线封存 2.430 s；两路同时申请时，新 episode 从 reset 到可用为 2.064–4.070 s，其中 VMM snapshot load、磁盘重新绑定、resume 和 ready 检查合计 0.181–0.252 s。`baseline_validate` 现阶段包含源锁等待与完整内存/磁盘 SHA 校验，不能把该字段全算成实际哈希耗时。实验机文件系统不支持 reflink，本次准备态 OverlayBD 增量层实际走 sparse-copy。它证明了运行状态和服务初始化的功能复用，尚未证明短初始化任务的启动或内存成本优于冷创建。该功能保持 opt-in；没有替换默认创建、ready 池或 EROFS 按需加载路径，也没有重新跑模型训练或声明 GRPO 学习收益。

首次验证中发现并修复了 root 增量层 hardlink 限制、私有 umask 导致 ublk 无法读取复制层、以及 privileged launcher 的日志名契约不匹配。当前通过现有存储组权限和 `restore.log` 标准恢复入口工作，无需修改 sudo/netns helper。失败结果保留在实验机 `episode-fork-validation-r1` 到 `r4`，这些失败尝试不计入四个成功 episode。


### 准备态共享 lower 与并行分叉优化（2026-10-06）

完整 snapshot SHA 校验移到封存时；同一 daemon 内的分叉只检查已验证文件的 inode、大小、mtime、ctime。daemon 重启后第一次使用基线重新完整验证。源锁仅保护基线检查、固定恢复设备准备与 reader pin，网络、私有 upper、snapshot load 和 guest 身份初始化在锁外并行进行。源 stop/TTL 等待未完成的 reader；分支完成恢复后，其磁盘共享对象由持久引用保护，内存 backing 由内核映射保留。暂停恢复仍使用分支自身的检查点。

`episode-fork-cow-validation-r1/result.json` 真实验证通过：同一个 TB2.1 `openssl-selfsigned-cert` 基线生成两轮 × 两个独立分支，全部使用同一个准备层路径/inode，四个私有 ublk 设备互不相同，分支没有复制准备层。官方 verifier 得 1 分；HTTP 内存状态独立、同路径写入隔离、下一轮无答案、源删除后的子分支暂停恢复、最后引用释放后的对象回收均通过。

| 同任务同机的小样本指标 | 首版 | COW/并行版 |
|---|---:|---:|
| 冷创建 | 1.551 s | 1.509 s |
| 一次性封存 | 2.430 s | 3.514 s |
| 分支端到端（包含探针观测） | 2.064–4.070 s | 0.925–1.155 s |
| reset 返回可用 | 首版未单列 | 0.921–1.150 s |
| 两路全部可用（包含观测） | 约 3.92–4.07 s | 1.078–1.155 s |
| 已封存基线复核 | 首版字段混入排队/全量 SHA | 1.88–6.07 ms |

首轮第二条源锁等待 66.4 ms（固定设备只创建一次），后续两条约 0.002 ms；当前主要创建阶段为磁盘/netns 准备 0.404–0.737 s，VMM 恢复 0.139–0.363 s、身份初始化 0.076–0.095 s。数据只有四个分支，不代表大并发吞吐结论或 60 ms 达标。封存成本前移且略有增加；短初始化任务仍需多 episode 摊销，不能把一次性成本忽略。该优化不涉及推理 MTP、GPU 训练内存或 GRPO 算法。

重启验证初次发现 worker `initialize` 未恢复已经写入的 `baseline_sealed/baseline_rollout_id/baseline_sandbox_id`，导致重启后拒绝新分叉，已有运行分支仍可执行。已补三字段恢复，并将 worker 基线声明与 daemon 的 PAUSED/sealed 状态交叉核验；不一致时保留 UNKNOWN，不恢复可写执行权限。失败证据保留在 `episode-fork-cow-restart-r1/result.json`。


修复后的 `episode-fork-cow-restart-r2/result.json` 通过真实受控 daemon/worker 重启：原分支 VMM PID 保持、内存 token 和服务计数延续、私有文件保持；封存基线加载后可继续生成干净分支。删除源之后，原分支再次暂停恢复及新分支持续执行通过。重启后首个分叉的完整校验耗时 1.399 s、core 分叉合计 2.267 s，这是有意保留的恢复校验成本，不纳入 0.92–1.15 s 的稳态成绩。此前 r1 重启失败证据未删除。

当前完整运行时版本为 `release-episode-fork-cow-r2`（125 文件，runtime profile），回滚备份 `episode-fork-backup-20261005T162114Z`。本地相关回归 41 项（1 项平台跳过）、打包回归 1 项通过；部署 Linux 回归 35 项通过。性能/官方 verifier 验证来自 cow-r1 完整版本，r2 修复 worker 重启恢复并加入相同 CAS 对象的重复 lower 引用去重，受控重启验证来自 cow-r2；不混用失败探针作为成功样本。验收后 active/pending/live 均为空、共享准备对象和引用计数均为 0，3FS 服务健康，磁盘剩余约 97.6 GiB。未启动 GPU 训练。
