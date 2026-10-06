# DSec 与 RL 训练框架的接入边界

本项目的 sandbox、资源调度、环境目录和 rollout worker 不依赖某个训练框架。训练框架适配器把一次策略 rollout 映射为持久的 `rollout_id`，经 `ScheduledDSecClient` 创建或重连沙箱、执行带 `step_id` 和 `action_id` 的动作、取得环境观察与可信评分。训练框架负责模型推理、token ID、logprob、response/loss mask、权重版本及参数更新；这些数据不能从 DSec 保存的对话文本反向补造。

DSec 发行保留薄框架适配器与统一环境协议。本文的 Miles CPU/GPU 内存补丁和探针属于训练侧实验及上游修复候选，默认运行时包和核心 wheel 排除；仅用 `--profile training-experiment` 打包复现，不作为 DSec 发布功能，也不作为沙箱优势证据。

| 训练入口 | 当前状态 | 最薄接入点 |
| --- | --- | --- |
| Miles | 已有 `miles_dsec_agent_function.py` 与 generate/reward 护栏；Qwen3.5-4B thinking 在非 TB2 任务取得可信 1 分且 TITO 完整。官方镜像配合 decoder 生命周期与 Miles 单 rank NCCL 卸载补丁，已完成两轮推理、Megatron LoRA 更新和 SGLang 恢复；两次可信 reward=1，参数差异已核验。 | 保留 Miles session server 的 token/logprob 轨迹，DSec 提供沙箱与可信判定；短计数任务的完整循环已通过；89 个 TB2.1 环境已接入正式入口，其中一个任务已有真实模型官方 1 分。Qwen3.5-2B 已完成正式 TB2.1 同任务两轮推理→训练→恢复及完整审计（有效 0/0 分、零梯度，无学习更新）；4B 随后切换到 FA2＋输出投影分块，正式 TB2.1 同任务两轮正分训练通过（1/1 分、非零梯度、192 个 adapter 张量变化）；尚不代表全任务、32768-token 实际训练或长期学习收益。 |
| verl | 尚无适配器或训练验证。 | 自定义 `AgentLoopBase.run`（必要时自定义 AgentLoopManager），使用 DSec worker 执行动作并将完整轨迹、mask 和奖励交给 verl。 |
| Uni-Agent | 尚无适配器或训练验证；此处指 `verl-project/uni-agent`。 | 自定义 Agent Runner 使用 Uni-Agent Gateway 的模型会话，使用 DSec worker 作为沙箱，并返回其 `TaskResult`。 |

在 Miles 的有效学习烟测通过后，优先实现 Uni-Agent 适配器，因为它已有 Gateway 与 verl 的训练轨迹通路。直接接入 verl 时可复用同一个 DSec episode 客户端，但仍需要一个 verl 专用 AgentLoop，不应假设 Miles 返回格式可直接使用。这里的“复用”是共享 DSec 沙箱服务，不是把三个训练框架的采样协议混为一个。

适配器必须遵守以下规则：

1. 一个训练样本对应一个稳定的 `rollout_id`；动作使用稳定的 `step_id`/`action_id`。请求结果未知时先重连、查询与协调，不能盲目重放可能有副作用的命令。
2. 只有 verifier 明确给出该任务的可信结果，样本才能进入训练；verifier 缺失或报错不等同于任务失败的 0 分。Uni-Agent 的空 `TaskResult.reward` 可能被后续奖励通路当作零值，因此此处必须显式拒收整条轨迹。具体任务的 verifier 由环境适配器提供，DSec 通用层不硬编码 TB2。
3. worker 可以恢复沙箱 episode。若训练侧会话重启后丢失既有 token/logprob/mask，恢复后的 episode 可以完成和清理，但该训练样本必须丢弃。只有训练侧也恢复完整轨迹，才能重新纳入训练。
4. DeepSeek API 等外部推理可以用于 harness 评估；若请求绕过训练框架的轨迹采集 Gateway，不能把结果直接用于依赖 token/logprob 的策略更新。

当前已从 Miles 适配器抽出 [AgentEnvironment 协议](AGENT_ENVIRONMENT_CONTRACT.md)：`reset/step/evaluate/stop`、通用动作/观察/评分对象，以及独立的 TB2 与通用计数任务插件。Miles 继续负责 bash 回复解析及 TITO；任务目录、WORKDIR、profile、资源和 `tb2_evaluate()` 已移到 TB2 插件。Miles 通过显式任务插件注册表选择环境，默认 TB2；生成/reward 护栏核对插件与评分器。新建 episode 和训练端重连的真实 TB2.1 microVM 冒烟、非 TB2 的正分通用 VM 冒烟、Miles 确定性策略回调的正分 episode，以及 Qwen3.5-4B 的正分完整 TITO 均已通过；正式 worker 已部署该协议并通过计数任务、Miles 确定性策略回调与受控 worker 重启验收，详见 [正式接口部署记录](AGENT_ENVIRONMENT_CONTRACT.md#正式接口部署与验收2026-10-05)。TB2.1 已注册到正式通用 worker/daemon，沿用独立 netns；真实 SDK 官方评分与证据保存通过，不依赖 OpenEnv。当前已核验两步有限非零梯度、实际 adapter 参数变化和每步训练后推理恢复。后续适配规划仍是给 Uni-Agent 增加薄 Agent Runner，最后按需求补原生 verl AgentLoop。每个入口都要通过同一组真实沙箱验收：正常 episode、动作超时后的幂等恢复、verifier 错误时样本拒收、训练侧重启后的轨迹处理，以及有效参数更新。现有 Miles 单步 0 分训练仅覆盖前述链路的一部分。

官方扩展接口：[verl How to Extend](https://verl.readthedocs.io/en/latest/extend_guide.html)、[Uni-Agent Gateway and Trajectories](https://github.com/verl-project/uni-agent/blob/main/docs/source/concepts/gateway-and-trajectories.md)、[Uni-Agent 仓库](https://github.com/verl-project/uni-agent)。

## 待评估上游 PR：Qwen decoder 初始化显存峰值（2026-10-05）

状态：本地补丁已验证初始化通过；保留为上游 PR 候选，尚未提交 issue 或 PR。2026-10-05 核对的 [Megatron-Bridge 官方 main 源码](https://github.com/NVIDIA-NeMo/Megatron-Bridge/blob/main/src/megatron/bridge/models/qwen_vl/modelling_qwen3_vl/text_model.py) 仍有相同构建顺序；本次检索未找到直接对应的官方确认或修复，不能据此断言不存在相关讨论。main 是可变引用，提交前需要重新核对并固定 commit。

问题位于 `Qwen3VLGPTModel.__init__`：`super().__init__()` 已创建 `self.decoder`，随后 `self.decoder = Qwen3VLTransformerBlock(...)` 先构建右侧对象才替换旧引用，导致旧 decoder 与新 decoder 在初始化期间共存。我们的 Qwen3.5-4B 路径复用了这个类；这不是 DSec 沙箱分配，也不是训练反向传播造成的峰值。

最小修改：

```diff
 # rebuild the transformer block
+del self.decoder
 self.decoder = Qwen3VLTransformerBlock(
```

实现保存在 [text_model_release_decoder.py](experiments/openenv_api/text_model_release_decoder.py)。[探针脚本](experiments/openenv_api/run_official_miles_counter_lora.sh) 通过 `DSEC_FIX_DOUBLE_DECODER=1` 将该文件只读挂载到容器的 `/usr/local/lib/python3.12/dist-packages/megatron/bridge/models/qwen_vl/modelling_qwen3_vl/text_model.py`；关闭开关即回到镜像原实现。这个整文件覆盖只针对当前固定镜像，不能直接覆盖不同版本；上游 PR 应只提交最小变更及必要测试。未改模型结构、checkpoint 映射或训练算法；释放发生在 checkpoint 加载前。旧张量释放后显存可供分配器复用，不代表 `nvidia-smi` 的 reserved/进程占用会立即同幅下降。

复现环境：RTX 4080 16 GiB、Qwen3.5-4B、TP/PP/CP 均为 1、BF16、Megatron LoRA。使用 Miles v0.1.0 镜像 digest `sha256:45ae7833f97a01369dbd8f336784830a87d94587bc010c7b8065291cc15cd7ca`（镜像内 Miles commit `78527b9102b3a5f15891128cbec3ac6c2dc4089e`、Megatron-Bridge 0.5.0、Core 0.16.0rc0）。执行镜像地址使用 `docker.1ms.run/radixark/miles` 镜像代理。

已取得的证据：

- OOM 时按实际分配调用点和 alloc/free 事件归类：父类语言模型约 7.834 GiB、MTP 0.225 GiB、视觉模块 1.228 GiB、正在构建的替代 decoder 4.235 GiB，总 requested 约 13.521 GiB。后者仅完成部分层就失败，不能称为一份完整 decoder 的大小。PyTorch allocated 约 13.57 GiB，与 requested 口径有分配对齐差异；视觉模块构建时也不是 checkpoint 的 BF16 文件大小口径。
- 修补后模型构建完成，记录的 allocated 为 9,419,823,616 bytes（约 8.77 GiB）。这是构建完成时读数，不能直接当作全过程峰值或与 OOM 读数相减宣称精确节省量。
- 后续配合磁盘 offload、仅对 decoder MLP 注入 LoRA、`qkv-format=bshd` 和 `attention-backend=unfused`，探针得到可信 reward=1、295 个 response token/logprob、269 个有效 mask 位、无 TITO 前缀不匹配；训练记录 `grad_norm=2.4586777742111514`、`valid_step=true`，并保存 adapter。之后 SGLang 恢复 KV 内存时 OOM，进程未正常完成；该次运行未完成循环；随后参数差异和循环恢复已在下文的 NCCL 修复运行中核验。这些额外配置不是 decoder 补丁的一部分。
- 原始失败日志目录：实验机 `/home/xiaoxiaohu/dsec-reproduce/rl-rejoin-p0/miles-official-v010-counter-lora-r2/`。分配快照：本机 `.runtime/miles_official_v010_oom.pickle`，SHA-256 `505a8166e7da93f56c682cff79f60a42aa0b7898218c68390050b0a87a193384`。后续训练日志和 checkpoint：实验机 `/home/xiaoxiaohu/dsec-reproduce/rl-rejoin-p0/miles-official-v010-counter-lora-unfused-r1/`。

提交 PR 前需要完成：

1. 固定上游 commit，提取不依赖 Miles、Ray 或 DSec 的最小模型构建复现，重新检查是否已有修复。
2. 在相同进程条件与配置下对比补丁前后 `max_memory_allocated` 和成功/失败情况，保留环境、参数及原始数据。
3. 验证 checkpoint 加载后的参数、固定输入前向输出和反向梯度一致性。删除旧 decoder 预期不改变 RNG 消耗顺序，但现有端到端运行不能替代数值对照。
4. 检查 PP/MTP、共享参数、CPU/meta 初始化及异常构建等相关路径；评估上游是否更适合让父类直接接收 decoder 类型以避免重复构建。
5. PR 只描述并修复初始化生命周期问题，不混入显存 offload、注意力后端和 SGLang 恢复问题。提交前再由用户决定是否发布。

## SGLang 恢复显存定位（2026-10-05）

范围纠正：用户明确要求先定位训练切回推理时仍驻留的显存，不能通过压低 SGLang 配额掩盖它。下述 0.7/分块实验仅保留为历史排查证据，**不作为已接受的修复**。`miles-counter-m70-c64-cycle2-r1` 已按此纠偏停止并清理临时卸载文件，没有完整验证结论；不再延伸第二轮训练调参。纠偏后的定位目标是卸载后约 2.14 GiB 增量的分配来源、持有者与释放路径；现已找到主要来源并完成下文两轮验证。

`miles-official-v010-counter-lora-unfused-r1` 已核验存在实际更新：`adapter_model.bin` 的 192 个张量均有限，其中 96 个 LoRA B 张量全部非零，最大绝对值约 `9.98378e-7`；当前 Bridge LoRA B 默认零初始化，且本次没有加载 adapter checkpoint。`training_state_rank0.pt` 记录 optimizer step=1。日志中的 lr=0 是一步线性衰减调度完成后的读数，不能据此误判该步没有更新。

在相同 0.8 静态显存比例下，追加“同步返回后清理”的隔离对照 `miles-official-v010-counter-lora-sync-reclaim-r1` 仍然在 KV 恢复时 OOM，exit_code=1。对照入口为 [miles_train_sync_reclaim.py](experiments/openenv_api/miles_train_sync_reclaim.py)，由 `DSEC_RECLAIM_AFTER_SYNC=1` 启用，默认关闭；这是排除假设的诊断补丁，不是已证实有效的修复。

关键口径：

- `after update_weights` 打印位于 `torch_memory_saver.disable()` 临时池内部，此时整卡占用约 15.06 GiB；退出该 context 后临时池自动销毁，占用已降至 12.68 GiB。不能把 15.06 GiB 误当成 `onload_kv` 开始时的常驻值。
- 同步返回后的 `clear_memory()` 前后都是 12.68 GiB，free=2.89 GiB，证明额外 empty_cache 不解决本次容量不足。
- SGLang 在启动时按比例 0.8 分配了 KV K/V 各约 0.91 GiB，以及 Mamba conv/SSM 各约 0.04/1.64 GiB，合计约 3.50 GiB。恢复需要的缓存容量超过上述 free，缺口约 0.61 GiB（各项日志有四舍五入）。CUDA graph 在本探针中已禁用。
- 两边模型均卸载时，整卡占用从首次训练前的约 2.02 GiB 增至训练后的约 4.16 GiB。NVML 采样定位增长主要在训练进程：SGLang 卸载时约 584 MiB，训练后训练进程 sleep 阶段观测约 4056 MiB；与 CUDA API 总量的采样时刻和口径不同，不能直接相加代替同步边界读数。这一步尚未归类到具体 CUDA 库；下文的原生分配追踪进一步定位了持有者。

已放弃的绕过尝试：当时尝试减少推理缓存预算，给训练后常驻开销留出空间；这没有解决生命周期遗漏。`miles-official-v010-counter-lora-mem70-cycle2-r1` 将 `DSEC_SGLANG_MEM_FRACTION=0.7`，连续执行 `DSEC_NUM_ROLLOUT=2`，同步后清理补丁关闭。KV 约 1.02 GiB（33,247 token），Mamba 约 0.96 GiB，合计约 1.98 GiB。第一轮训练后成功恢复推理，第二轮实际完成两次工具调用并取得可信 reward=1，只证明减少缓存后可以绕过该次恢复 OOM；两轮 response 分别 295/237 token，TITO 完整。

该运行尚未完整成功：第二轮 Megatron 计算 logprob 时，Triton 自动调优复制约 226 MiB 词表张量引发 OOM，记录 free=1.08 GiB，训练配置另有 `train_memory_margin_bytes=1 GiB`。这不是同一次 KV 恢复错误。当时进一步尝试官方 `--log-probs-chunk-size 64`，已按用户纠偏停止，不作为修复；最终成功运行没有启用分块，也没有降低 SGLang 配额。

### 残留显存的分配归属与生命周期遗漏

保持 SGLang 0.8 和原始不分块 logprob 配置，`miles-offload-owner-audit-r1` 在首次训练卸载后停止，只读取分配元数据。使用与已安装库相同 commit `74d68c5e4bedf2b6774f2c92ed0f81b7c8d91ed0` 的 torch_memory_saver 头文件，读取其现有 singleton 的分配表；没有替换 allocator。结果：训练前后仍 ACTIVE 的均为 `param_buffer` 16 MiB、`grad_buffer` 32 MiB，default 区域分别 9066/9406 MiB 均已 PAUSED；PyTorch 快照没有未归属到这些区域的活跃张量块。因此多出的约 2.14 GiB 不是漏卸载的模型参数或 LoRA 梯度。

`miles-offload-native-audit-r1` 进一步用 CUPTI 记录 CUDA driver 分配/释放调用，在同一卸载边界得到：

- 首次训练前已持有 2 块各 512 MiB 的 NCCL 原生分配；训练后仍持有 6 块，新增 **4 × 512 MiB = 2 GiB**。NCCL 版本 2.28.9；调用栈解析到 `ncclProxyService → proxyProgressAsync → proxySharedInit → sharedNetBuffersInit → ncclCudaCallocDebug<char> → cudaMalloc`，是网络代理共享缓冲区。
- 这里的“4”是新增 CUDA 分配块数，**不是通信组数量**。修复运行的日志显示该单 rank 训练进程重建了 17 个注册的 process group；Megatron 为不同并行与同步用途仍创建逻辑组，尽管每组只有一个 rank。现有 CUPTI 分配记录未携带通信组 ID，无法证明 4 块缓冲区与其中 4 个组一一对应。
- 另有新增的 2 × 8 MiB cuBLAS 原生分配，调用栈含 `cublasCreate_v2`、`at::cuda::getCurrentCUDABlasHandle`。默认 CUDA 内存池 reserved/used 在卸载边界均为 0；不能将余量归咎于默认异步内存池缓存。
- 2 GiB NCCL 加 16 MiB cuBLAS 解释了增量中的主要部分；剩余约 0.12 GiB 尚未完整细分，不把全部 2.14 GiB 都算成 NCCL 缓冲区。

直接对应的 Miles 代码是 `miles/utils/reloadable_process_group.py` 的 `if len(ranks) == 1: return group`。单 rank NCCL 组因此没有注册到 `ReloadableProcessGroup.GROUPS`，`sleep()` 中的 `destroy_process_groups()` 不会销毁这些组。它们仍然拥有实际 NCCL 资源，这属于当前固定 Miles 版本的生命周期遗漏，不能表述为已证实的 NCCL 自身内存泄漏。

2026-10-05 通过 GitHub API 核验：官方 main 当时为 [`209bdbd1c280bf3dffd0a61cb9def25abdd991a5`](https://github.com/radixark/miles/blob/209bdbd1c280bf3dffd0a61cb9def25abdd991a5/miles/utils/reloadable_process_group.py#L41-L44)，仍在第 41–42 行跳过单 rank 组；我们镜像的固定 commit 也存在相同逻辑。这是源码层面的确认，未对最新 main 重新执行 GPU 复现，不能宣称其所有配置都会 OOM。本次检索未找到 Miles 针对此单 rank 卸载遗漏的直接确认或修复；相关 PR #3688 解决的是 torch 2.13 backend dispatch，不能混作此问题的修复。NCCL 官方已有 [issue #2363](https://github.com/NVIDIA/nccl/issues/2363)，报告同为 NCCL 2.28.9 / PyTorch 2.11 / CUDA 13 的单 rank 组分配大块 NET shared P2P 缓冲区，维护者已回应将评估资源开销优化。该报告支持底层分配机制，但与 Miles 漏纳入卸载管理是两个层次的问题。官方两个版本源码已保存于本机 `.runtime/miles-upstream-nccl-check/`。

修复候选 [miles_reloadable_process_group.py](experiments/openenv_api/miles_reloadable_process_group.py) 将单 rank NCCL 组也纳入销毁/重建，只跳过 `NON_GROUP_MEMBER`；探针开关为 `DSEC_FIX_SINGLETON_NCCL_OFFLOAD=1`。独立单 GPU 测试已验证两次 all_reduce 均保持张量值 3，组销毁后整卡占用从约 900.81 MiB 降至 386.81 MiB，重建后再通信、再销毁也回到相同读数。完整两轮验证 `miles-singleton-fix-cycle2-r1` 已通过，保持 SGLang 0.8、不启用 logprob 分块、不启用同步后额外清理。该补丁仍是固定版本的显式探针开关。已按官方 main 整理最小改动与 GPU 回归测试，发布草稿 [Miles #3914](https://github.com/radixark/miles/pull/3914)（commit `98942681e85ec38a312091771e04c1736ec583c3`）。全仓库 pre-commit 已通过，测试已出现在 H200 CI 选择计划；完整 CPU 和多 GPU 验证仍待完成。

原始证据均在实验机 `/home/xiaoxiaohu/dsec-reproduce/rl-rejoin-p0/` 下相应目录的 `allocations/`：`*.tms.json`、`*.tensors.json`、PyTorch `*.pickle` 和 `cuda-native.jsonl`。两次归属探针的 exit=0 仅代表诊断完成，它们刻意在卸载后停止，不能作为完整训练恢复成功的证据。


### 原配额两轮完整验证通过

2026-10-05，实验机目录 `/home/xiaoxiaohu/dsec-reproduce/rl-rejoin-p0/miles-singleton-fix-cycle2-r1/` 的 `cycle-audit.json` 记录 `status=passed`、`exit_code=0`，墙钟 253.282 秒。执行配置：

```bash
DSEC_FIX_DOUBLE_DECODER=1 DSEC_TRAIN_DISK_OFFLOAD=1 \
DSEC_SGLANG_MEM_FRACTION=0.8 DSEC_FIX_SINGLETON_NCCL_OFFLOAD=1 \
DSEC_NUM_ROLLOUT=2 bash /home/xiaoxiaohu/dsec-reproduce/rl-rejoin-p0/agent-env-contract-20261005/code/run_official_miles_counter_lora.sh \
  /home/xiaoxiaohu/dsec-reproduce/rl-rejoin-p0/miles-singleton-fix-cycle2-r1
```

- 两个 episode 都由真实 Qwen3.5-4B thinking 执行并取得可信 reward=1；response/logprob 分别为 295/295 和 174/174，有效 mask 为 269 和 160，均无 TITO 不匹配。
- optimizer step 分别为 1、2；梯度范数为 2.4586777742111514、4.135039394795265，均有限且非零。两个 checkpoint 的 192 个 adapter 张量全部发生变化，包含 96 个 LoRA B；最大步间差异 `4.991888999938965e-7`。
- 每次训练后都完成 SGLang 权重和 KV 恢复。两侧卸载时的整卡占用，修复前训练后约 4.16 GiB，修复后两轮均约 1.66 GiB；修复后首次训练前约 1.52 GiB。该约 2.50 GiB 降幅包含训练前已有的可回收通信缓冲区，不能与“训练新增的 2 GiB NCCL 缓冲区”混为同一指标。
- GPU 定时采样峰值 14,550 MiB，容器内存记录峰值约 26.70 GiB；GPU 采样不能替代瞬时分配峰值。运行结束 GPU 回到约 35 MiB，临时磁盘 offload 文件已清理。

独立通信组销毁/重建回归脚本为 [probe_miles_singleton_nccl_offload.py](experiments/openenv_api/probe_miles_singleton_nccl_offload.py)。本结果只覆盖单卡、短计数任务、max sequence 2048 / response 1024 的两轮循环；不代表 32768 response、TB2.1 全任务、长时间训练或多卡组合通过。它证明当前恢复故障可通过修复 Miles 生命周期解决，无需削减原来的 SGLang 0.8 配额。


### 正式 worker 的真实两轮训练（2026-10-05）

Qwen3.5-4B thinking 已通过正式 SDK/worker 完成两轮计数任务推理→训练→恢复推理：两次 reward=1，295/174 response 的 TITO 对齐，有限非零梯度，两步 checkpoint 的 192 个 adapter 张量实际变化，exit=0，训练进程墙钟 248.946 秒。模型与 SGLang 0.8 配额保持原值。首试第二轮被 4 GiB host 保留配置阻塞；保存中断证据后，在空闲时将 host 保留值调整到 2 GiB，重跑通过，任务预算保持原值。该调整没有减少训练主存用量。详见 [正式训练记录](AGENT_ENVIRONMENT_CONTRACT.md#真实-miles-训练接到正式入口2026-10-05)。这关闭了计数任务实际模型训练的正式入口验收；后续 Qwen3.5-2B 在正式 TB2.1 上完成两轮训练链路验收，回复上限为 32768，实际有效轨迹约 5K token，官方 0/0 分、零梯度；4B 随后以 FA2＋输出投影分块通过正式 TB2.1 两轮正分训练，详见下文；尚未覆盖全任务。

### 主存原因已定位，LoRA-only NVMe 候选通过两轮（2026-10-05）

主因是 `colocate` 仍启用完整 actor pinned CPU 权重备份，独立于已启用的训练磁盘 offload。实测 8.70 GiB 张量经 pinned allocator 逐个取整后约 12.03 GiB；改为仅保留 16 MiB LoRA 的 CPU 同步副本，并由 SGLang 从 NVMe 恢复冻结基础权重后，两轮真实推理→训练→恢复通过，三次恢复的 627 个基础参数全量哈希一致。容器总峰值 27.24→23.43 GiB；Megatron 初始化后的 host 最低可用内存采样 4.06→12.11 GiB。六项条件检查通过。详见 [主存定位报告](MILES_HOST_MEMORY_REPORT.md)。

该路径是固定版本、单 rank 的可选 `DSEC_LORA_ONLY_NVME=1`；正式预算没有改动。候选受正式入口磁盘保留阈值阻塞后，在独立有界 worker 上完成验证；额外磁盘余量条件仍须在正式入口满足。当前 TMS 仍每次 pause 写入基础权重，尚未实现不可变备份只写一次，也未证明关闭审计后的整体吞吐提升。RL 算法为 REINFORCE++ 单样本链路验证，不是 GRPO 学习效果测试。


## FlashAttention 临时绕过的纠正（训练实验，2026-10-05）

早期 `miles-official-v010-counter-lora-bshd-r1/train.log` 显示 TE 自动选择 FlashAttention 4，失败调用栈为 `flash_attn/cute/pack_gqa.py → crd2idx` 的 CUTLASS MLIR 编译错误。当时为完成短计数任务切换到 unfused，但该临时配置沿用到 TB2.1 长轨迹，导致完整注意力矩阵带来显存峰值。FA4 失败不能证明 FA2 不可用。

固定 TE 2.17 镜像的后端选择没有独立 FA2 环境开关；实验启动器通过 `DSEC_TE_FLASH2_ONLY=1` 加载严格锚点生成的任务私有 TE utils 补丁，仅排除 FA3/FA4 候选，训练参数设为 `--attention-backend flash`。没有升级或卸载包，关闭开关即可恢复原镜像。此项不进入 DSec 默认发布。

`flash2-attention-probe-r17.log` 在 RTX 4080、BF16、16 Q heads/4 KV heads、head dim 256、BSHD、causal/dropout 0 上通过。实际后端为 FA2 2.7.4.post1；129-token 前向最大差 0.015625、梯度最大差 9.536743e-7，均在探针容差内，不能宣称逐位相同。8192-token 前向和非零有限梯度通过，探针额外 allocated 峰值 738,723,840 B（704.5 MiB），不是完整模型训练峰值。后端对照分进程运行，避免 TE 对相同输入元数据缓存先前 backend。

`miles-openssl-4b-flash2-cycle2-r1` 使用独立 `release-training-flash2-r17` 实验包开始真实 TB2.1 两轮训练，保持 Qwen3.5-4B thinking、32768 response、65536 context、原采样、SGLang 0.8 和正式 DSec worker。真实运行已通过严格 `positive_training_updates` 审计：exit=0，441.612 秒；reward=1/1，response 4657/2729 token；梯度范数 0.1018514100/0.8552961829；两 checkpoint 间 192 个 adapter 张量变化（96 个 LoRA B），最大差 4.991889e-7，无被排除的 episode。两个 job-owned 沙箱最终 STOPPED、pending=null、lease=false。第一步更新后的第二次真实生成验证推理恢复；第二步后的 adapter 同步、继续生成接口成功，随后正常退出，未生成第三个任务。GPU 采样峰值 14148 MiB、训练容器 memory peak 16.352 GiB，不包含 DSec 后端全成本。GPU 回到 35 MiB，临时 offload 删除，磁盘余量约 70.07 GiB。审计副本在 `.runtime/tb21-formal-entry-20261005/4b-flash2-cycle-audit.json`。这证明有效训练更新闭环，不证明学习收益。


### 输出投影分块消融（2026-10-05）

`ablation-flash2-no-output-chunk-r1` 使用官方 `--load-debug-rollout-data`，重放上述 RL 第一条原始轨迹（rollout `da1902dc1c774fa493b107ad61e71d72`；4657 response/5082 total token，输入文件 SHA-256 `c53eda42356f1f58754e659ecb7b289b754e12e5d5e7cfbd06e72cd9e21fd2cc`）。保留原 4B、FA2、BF16 logits、64-token CE 分块重计算、LoRA、recompute、NCCL/decoder 修复与默认训练显存余量，仅取消输出投影分块。此为训练侧消融，不启动 SGLang、不调用 agent 或 verifier，也不产生新的 RL episode；与完整 RL 的 GPU 总占用不能直接对比。

原生 teacher-forcing logprob 计算完成，均值与原 RL 一致；随后训练 loss 阶段 OOM（32 MiB 分配，日志 free 1.03 GiB、allocated 12.42 GiB、reserved-unused 237.60 MiB，非 PyTorch 开销包含在进程总占用 14.50 GiB 中；训练保留的默认 1 GiB 余量未降低）。exit=1，104.857 秒，无 optimizer checkpoint。即使没有 SGLang 驻留仍失败，说明 FA2 并不替代该配置的输出投影分块。完整词表输出及其梯度仍随 token×vocab 增长。当前保留已经完成正分 RL 验收的组合；本消融未单独证明 CE 重计算、BF16 后处理等每个补丁都不可删除，也未证明更大 GPU 必须分块。失败日志和计划保留，临时 train-offload 已删除。


### 独立 CE 重计算消融（2026-10-05）

保持同一原始轨迹、FA2 与输出投影分块，独立 CE 开关开/关均完成一次训练并保存 checkpoint。两次 loss 相同，GPU 采样峰值均为 13487 MiB，梯度均非零；梯度范数相对差约 0.076%，adapter 最大绝对差约 1.997e-6，不能宣称逐位一致，数值差异来源未单独定位。本对照为离线训练重放，不启动 SGLang，不是新一轮完整 RL 验收。

源码确认：分块 helper 已对“输出投影＋CE”整体 checkpoint，内部 CE 调用未传入 `recompute=True`；分块 logit processor 提前返回，额外独立 CE checkpoint 未被执行。因此 TB2.1 实验启动器在输出投影分块时默认关闭独立 `DSEC_RECOMPUTE_LOGPROBS`，完整 logits 路径仍默认开启，显式覆盖仍有效；整体 checkpoint 保留。r18 是 training-experiment 包，此优化不属于 DSec 默认发布。此前完整两轮 RL 的验收包仍是 r17，未将 r18 离线重放计作完整 RL。对照证据位于 `.runtime/tb21-formal-entry-20261005/ce-recompute-ablation-comparison.json`。

### GRPO 沙箱复用验收（2026-10-05，已通过）

`miles-openssl-4b-grpo-reuse-r2`（r22）已通过完整自动审计 `grpo_sandbox_reuse`：训练 exit=0，674.202 秒；两组各 4 条，官方 rewards 分别 `[1,1,1,0]` 与 `[0,1,0,1]`，全部 8 条 canonical/TITO 对齐，无丢弃 episode。两次 optimizer gradient norm 为 0.2456585199、0.2362498083；两个 checkpoint 间 192 个 adapter 张量实际变化（96 个 LoRA B），最大绝对差 4.991889e-7。首步更新后真实第二组生成通过，第二步更新后推理恢复接口成功，随后正常退出；没有第三组实际生成，不宣称学习收益。

8 个独立沙箱均完成 `RUNNING → PAUSED → RUNNING → STOPPED`：暂停时无活跃 VMM，恢复后 PID 改变，沙箱 ID 不变，snapshot generation=1，下一条模型工具命令正常执行；暂停包含探针 RPC 的墙钟约 2.17–3.05 秒。8 个 episode 共用同一组 6 个 EROFS 源文件，共 75,894,784 B（72.38 MiB），运行前后 inode/mtime/SHA-256 与 OverlayBD 基础源均不变；私有同路径写入与下一 episode 干净状态的独立检查通过。没有启用 ready 池，也没有跨 episode VM reset/fork；这次证明共享不可变环境和同 episode 的快照续用。

GPU 采样峰值 15432 MiB，训练容器 memory peak 19.649 GiB；这些统计不包含完整 DSec 后端成本，不能作为 Docker 对照。完成后 GPU 35 MiB、磁盘余量约 97.66 GiB，临时 train-offload/rollout-base-nvme 不存在，全部 job-owned episode STOPPED/pending=null/lease=false，调度器无 active/pending，3FS 客户端和服务端均 up。当前工作区保存 `.runtime/tb21-formal-entry-20261005/grpo-reuse-r2/grpo-reuse-audit.json` 与 `post-run-health.json`，全量 TITO、模型原文、verifier 和 checkpoint 留在实验机 r2 目录。

用户要求用 GRPO 工作负载证明 DSec 的复用能力。训练实验入口 `run_official_miles_tb21_grpo.sh` 沿用已通过的 Qwen3.5-4B、thinking coding 采样、32768 response/65536 context、FA2 与输出投影分块；正式 SDK/worker/daemon 和发布默认 runtime 未改动。每个 rollout step 为同一 prompt 的 4 个独立 episode，2 组，rollout 并发 2、训练 micro batch 1/global batch 4，通过梯度累积完成一组一次优化。官方 Miles 解析确认 GRPO、组内奖励和标准差归一化开启；没有复用 REINFORCE++ 的 gamma/额外 token 优势白化设置。

同任务私有写入检查已通过：两 VM 的同一路径写入不同 rollout marker 并各自读回；第二对全新 episode 不存在旧 marker；所有测试由正式 SDK 建立并停止。GRPO 的模型轨迹额外通过训练侧代理在第一条工具命令后暂停一次，再由下一条工具命令触发恢复，记录 worker sandbox_status、快照 generation、原/新 VMM PID、沙箱 ID、上下文、TITO 与官方 verdict，不改变 prompt、shell 动作或 reward。共享 EROFS 与 OverlayBD 基础源保留运行前后 SHA-256/inode/mtime 证据。

该验收区别三个边界：不可变环境跨 episode 复用、同一 episode 快照恢复、私有写入隔离；不将它宣称为跨 episode 运行态 VM reset/fork、ready 池命中或学习收益。组内奖励全部相同是 GRPO 的退化分组，须显式报告，不能修改评分或筛选成功样本来制造梯度。自动审计要求 8 条完整 canonical/TITO 轨迹、两个 checkpoint、无被排除的 episode、实际恢复和资源释放。

早期检查目录：实验机 `rl-rejoin-p0/tb21-formal-entry-20261005/miles-openssl-4b-grpo-reuse-r1`，不可变包 `release-training-grpo-reuse-r21`。22 项本地测试与 lint 通过。r19 启动器覆盖检查标志的试运行已停止并保留记录，修复后 r20 官方参数解析通过；新增回归检查保证磁盘 offload 不再覆盖 dry-run/复用探针标志。该轮未通过，最终真实 GRPO 验收为上文 r22/r2，未混入早期模型结果。

首轮发现复用代理调用了训练包装器，但漏掉普通 Miles 入口的 canonical verdict 标记，生成护栏因此拒收所有样本并持续补采；未形成完整 GRPO 分组或 optimizer step。停止后仅清理该 job 拥有的 14 个 episode 和临时卸载，TITO、模型原文、verifier 和恢复事件保留。共享的 `canonical_training_result()` 现用于普通与探针入口，回归覆盖有效 0/1 分均能通过生成护栏、无 verdict/旧 session 均拒收；24 项本地检查与实验机 23 项 adapter 测试通过。r22 新轮目录为 `miles-openssl-4b-grpo-reuse-r2`，全部模型结果重跑，不计入失败轮 episode。期间回收已安装软件的六个旧安装包 1.844 GiB；无活跃引用的旧宿主 Ray 错误日志经 gzip 与解压 SHA-256 校验后保留，净释放 1.479 GiB。清理后磁盘可用约 73.1 GiB，正式调度保留阈值未降低。
