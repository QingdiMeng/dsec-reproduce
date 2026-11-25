<a id="mbpp-with-dsec-and-verl"></a>

# 使用 DSec 和 verl 运行 MBPP

[English](README.md) · **简体中文**

这个可选应用准备原版 MBPP 数据集，并实现 verl 原生的异步 `custom_reward_function`。对于每个样本，DSec 调度一台新的 MicroVM，并在其中执行生成的 Python 程序和三条官方断言。verl 负责生成、token ID、logprob、GRPO 和模型更新。使用此适配器无需部署 OpenEnv、AgentENV 或 SandboxFusion 服务。

**已验证范围。** Qwen3.5-2B 在全部 374 道原始训练题上完成了一个 epoch 的原生 verl GRPO 训练，每题采样 8 次，共更新 187 次。训练前后各评测全部 500 道原始测试题，每题同样采样 8 次。真实 MicroVM 上的奖励、截止时间、隔离验收，以及同一批固定候选答案在 8/16/32/48 并发下的执行验证均已通过。此案例验证单轮代码生成。多轮 verl Agent Loop 和针对恶意评分器的生产隔离另需验证。

<a id="completed-case-and-results"></a>

## 已完成的训练与评测结果

SGLang 生成候选程序，verl 用原生 FSDP2/GRPO 路径更新 LoRA 适配器。对于每条可执行的回复，DSec 调度器创建独立的无网络 MicroVM，在其中运行 Python 和测试，再停止虚拟机。各 episode 共用不可变的 EROFS 镜像层，各自使用私有可写盘。本案例验证共享镜像的执行和资源准入，尚未验证准备态内存分叉、DAX 或 3FS。

验收使用单张 16 GiB 的 RTX 4080，模型为 Non-Thinking 模式下的 Qwen3.5-2B。LoRA rank 为 8、alpha 为 16，单次回复上限为 8192 token，有效生成并发槽为 32。每个可执行样本使用 1 个 guest CPU，配置 512 MiB guest 内存。这些是本次实测配置。

| 指标 | 训练前 | 训练后 |
| --- | --- | --- |
| 成功回复数（共 4000 条） | 1591 | 1691 |
| 样本平均成功率（pass@1 估计值） | 39.775% | 42.275% |
| 8 次采样中至少解出一次的题目 | 324/500 (64.8%) | 342/500 (68.4%) |
| 达到长度上限的回复 | 86 | 108 |
| 因模型输出格式错误得零分的回复 | 105 | 123 |

平均样本成功率提高 2.5 个百分点。按题配对做 bootstrap，95% 区间为 +0.975 至 +4.0 个百分点。41 道题从未解出变为解出，23 道题从解出变为未解出。前后测独立采样，均按三条原始公开断言评分。这是单次实验，未使用隐藏测试，也未做 Docker 与 DSec 的性能对照。重新运行后测时没有生成、基础设施或清理错误。实际创建的 3877 台虚拟机全部停止，其余 123 条回复因格式错误得零分，未创建虚拟机。汇总排除了首次失败的后测；恢复过程中未增加训练更新。[证据、对比方法和限制](../../docs/reports/MBPP_VERL_FIRST_USE.zh-CN.md)

依次安装可选应用、准备固定版本的数据、构建并启动 Python 沙盒、运行正确性验收，最后执行完整 epoch 的训练和前后测对比命令。仅安装 DSec 核心包不会安装 MBPP、verl 或模型权重。

<a id="prepare-explicit-dependencies"></a>

## 准备依赖

分别安装 DSec 核心包和 MBPP 应用。

```sh
python -m pip install .
python -m pip install './apps/mbpp[parquet]'
```

从[官方数据集](https://github.com/google-research/google-research/tree/e49bbfe381c9c0e564b937f1c4e163a2273c65cc/mbpp)下载 `mbpp/mbpp.jsonl`。应用按 SHA-256 校验文件，只有摘要为 `ccf64ceae9c5403bf50a044cb6d505bfd2a2963ee58338ba268fd65beab92a9f` 才会接受。安装不会下载任务、镜像、模型或训练框架。上游数据沿用其自身的许可条款，本应用的实现采用 MIT 许可证。

```sh
dsec-mbpp --input /data/mbpp.jsonl --out /data/mbpp-prepared --parquet
```

原版 MBPP 的划分按题号固定：601–974 为训练集（374 题），511–600 为验证集（90 题），11–510 为测试集（500 题），1–10 为少样本提示示例（10 题）。准备工具写出 JSONL、哈希清单，也可选写 Parquet。提示包含题目描述和三条原始公开测试，与 MBPP 的设定一致。这三条测试也用于计算执行奖励。参考实现 `code` 和挑战测试不写入输出。测试题不可参与训练，参考实现不可充当模型轨迹。公开断言的通过率只反映这些断言的覆盖范围。

<a id="build-and-start-the-shared-python-environment"></a>

## 构建并启动共享 Python 环境

按[核心快速开始](../../docs/guides/QUICKSTART.zh-CN.md)安装构建工具。准备可信且**同时支持 EROFS 和 OverlayFS** 的 guest 内核、Firecracker、静态 BusyBox、GCC 和静态 libc、Docker 及本地已有的 `mkfs.erofs` 工具镜像。普通 Firecracker guest 内核可能缺少 EROFS 支持，导致挂载失败，此时命令 agent 尚未启动。镜像在独立的离线步骤中构建，无需 TB2.1。

```sh
IMAGE_ID=$(docker image inspect python:3.13-slim-bookworm --format '{{.Id}}')
TOOLS_ID=$(docker image inspect your-local-erofs-tools --format '{{.Id}}')
dsec-prepare-image --image "$IMAGE_ID" --tools-image "$TOOLS_ID" \
  --environment-id python-mbpp --output "$HOME/dsec-artifacts/python-mbpp" \
  --kernel /opt/dsec/vmlinux-erofs --agent-source ./guest_agent.c \
  --busybox /usr/bin/busybox --cpus 1 --memory-mb 512 \
  --boot-size-mb 256 --reserve-gib 10
```

构建工具使用固定版本的 OCI 层，保留 whiteout 删除标记，生成已验证的环境目录及来源与哈希回执。镜像使用可变标签、输出已存在或剩余磁盘不足时都会拒绝构建。MBPP 的参考程序只需 Python 标准库。构建工具不应用 OCI 入口命令、`WORKDIR` 或镜像环境变量，其他应用如有依赖需显式配置。所有样本共享不可变镜像层，每个样本使用独立的可写启动盘，无需为 guest 联网。

用 `dsec-host --config host-mbpp.json init --instance mbpp-verl` 配置独立实例，传入 `--state-root "$HOME/dsec-state/mbpp"`、`--catalog "$HOME/dsec-artifacts/python-mbpp/catalog.json"`，将生成的 `boot.ext4` 用作 `--template`，VMM 和内核路径分别填入 `--binary`、`--kernel`。按快速开始依次执行 doctor、render、start、wait。通过 `ip route show default` 查出实际宿主机网卡并填入 `--network-interface`；即使 guest 离线，调度器也会测量该网卡。按宿主机实际资源设置容量和准入预算，默认的两个 episode 槽只作起步示例。这个离线实例无需网络 helper。为它使用独立的状态根目录，保留其他应用的服务配置。

```sh
dsec-mbpp-verify --api-config /run/dsec/api-client.json \
  --environment-id python-mbpp --out ./mbpp-acceptance \
  --samples 32 --concurrency 8 16 32
```

验收分别运行正确、错误、超时和提前退出的样例，并检查前一条样本创建的文件在下一台 VM 中是否确实不存在。每次执行的回执都会保存。各档并发复用同一组固定合成样本，用于测试系统执行能力。这些执行不能计入 MBPP 模型准确率或独立 RL 样本数。

运维人员还需部署[API、节点与 worker 桥接服务](../../docs/guides/CLUSTER_DEPLOYMENT.zh-CN.md#remote-native-sdk)，注册 `python-mbpp`，并声明与配方匹配的最低资源需求。应用获取的 `/run/dsec/api-client.json` 包含 API 地址、IAM 项目、token 和 CA 文件引用。奖励钩子接收 `api_config`，始终通过 API 通信。执行或评分结果未知时保留 episode ID，状态查询读取同一节点上的 worker 证据。上文训练数据来自早期通过本机 worker 通信的实验。

<a id="native-verl-integration"></a>

## 接入原生 verl

接口及依赖固定在 [verl 修订 8718ca3](https://github.com/verl-project/verl/commit/8718ca30a3f002f93b7c4fd99b9b2506718681bc)。该版本已在 GPU 上跑通四次更新的试验和完整 epoch。为 verl 及其模型和推理后端单独安装环境，保留现有的 Miles 环境。

```sh
export DSEC_API_CONFIG=/run/dsec/api-client.json
export DSEC_MBPP_ENVIRONMENT_ID=python-mbpp
export DSEC_MBPP_EVIDENCE_DIR=/data/mbpp-run/execution-evidence
```

按固定的上游修订创建独立的 Python 3.12 GPU 环境，供本案例使用。

```sh
git clone https://github.com/verl-project/verl.git verl
git -C verl checkout 8718ca30a3f002f93b7c4fd99b9b2506718681bc
cd verl
uv sync --frozen --extra sglang --extra fsdp
export CUDA_HOME=/usr/local/cuda  # Your compatible CUDA 13 development toolkit.
export MAX_JOBS=2
# Use the same interpreter for driver, Ray workers and the reward package.
uv pip install --python .venv/bin/python --no-deps /path/to/dsec_reproduce-0.1.0.dev0-py3-none-any.whl \
  /path/to/dsec_mbpp_case-0.1.0.dev0-py3-none-any.whl
.venv/bin/dsec-mbpp-train --verl-root "$PWD" --model /models/Qwen3.5-2B \
  --data /data/mbpp-prepared --api-config "$DSEC_API_CONFIG" \
  --environment-id "$DSEC_MBPP_ENVIRONMENT_ID" --out /data/mbpp-config-check --dry-run
.venv/bin/dsec-mbpp-train --verl-root "$PWD" --model /models/Qwen3.5-2B \
  --data /data/mbpp-prepared --api-config "$DSEC_API_CONFIG" \
  --environment-id "$DSEC_MBPP_ENVIRONMENT_ID" --out /data/mbpp-pilot --steps 4
```

依赖锁定为 CUDA 13、PyTorch 2.13、SGLang 0.5.20、Transformers 5.12.1。请准备兼容的 NVIDIA 驱动、带 C++ 编译器的 CUDA 13 开发工具包，供 FlashInfer 首次 JIT 编译使用。将 `CUDA_HOME` 指向工具包，并用 `MAX_JOBS=2` 限制小型宿主机的首次编译开销。启动器会将当前虚拟环境的工具目录放在 PATH 首位；仅运行该环境的 Python 无法让 Ninja 等命令行工具生效。`--dry-run` 只生成原生 Hydra 配置。实际运行命令验证四次更新，每个训练批次含两道题、每题生成八条回复；验证使用四道题，每题同样生成八条。`--steps`、`--batch-size` 和 `--validation-samples` 控制这次试验的规模。输出包括完整启动参数、训练日志、rollout 与验证记录、检查点和各样本的沙盒证据。生成记录保留原生提示与回复 token ID、回复掩码和 logprob、实际采样参数及结束原因；执行回执将生成 ID 与沙盒 rollout ID 关联，从而保留训练框架自身的 token 链路。每次尝试使用新的输出目录。已验证的组合使用 SGLang 生成、FSDP2 更新适配器；verl 仍需配置 rollout 后端，DSec 奖励钩子仅依赖统一沙盒 SDK。试验使用原生 FSDP 全状态检查点，该模型每份约 4.23 GiB，最终保留一份，请提前预留空间。早期试验使用上游默认的空洗牌种子，实际样本组和得分均有记录。现在启动器将数据集与 SGLang 种子固定为 42，可用 `--seed` 修改。固定种子仍不能保证采样逐比特一致。

<a id="complete-training-and-held-out-comparison"></a>

### 完成训练并比较留出集结果

对全部 374 道训练题各采样八次，跑完一个 epoch。默认每个批次两题，对应 187 次 GRPO 更新和 2,992 条训练样本。启动器校验数据哈希及题号。`--epochs` 会拒绝可能丢弃最后一个不满批次的配置；提示过滤已关闭，输入过长会显式报错。4096 token 的提示窗口可容纳原版测试集中最长的提示。以下配方允许最多生成 8192 token，有效生成并发配置为 32，总上下文为 12288 token。前测和后测使用相同限制。

```sh
.venv/bin/dsec-mbpp-train --verl-root "$PWD" --model /models/Qwen3.5-2B \
  --data /data/mbpp-prepared --api-config "$DSEC_API_CONFIG" \
  --environment-id "$DSEC_MBPP_ENVIRONMENT_ID" --out /data/mbpp-full \
  --epochs 1 --batch-size 2 --prompt-length 4096 --seed 42 \
  --response-length 8192 --generation-concurrency 32 \
  --gpu-memory-utilization 0.6 --mamba-cache-slots 160 \
  --logprob-chunk-size 128 \
  --evaluation-split test --validation-samples 500 --evaluation-batch-size 32 \
  --evaluate-before-train --checkpoint-every 20
.venv/bin/dsec-mbpp-compare --run /data/mbpp-full --out /data/mbpp-comparison.json
```

GDN 状态槽不足时，SGLang 的有效并发可能低于请求值。在固定版本的后端上，radix 缓存和重叠执行缓冲区合计使每个运行中的请求最多需要 5 个状态槽。这个配置为 32 个请求预留了 160 个槽，保持原有状态精度。通过 SGLang 的 `/v1/loads` 查看 `max_running_requests` 和 `num_running_reqs`。只改 `max_num_seqs` 不能保证达到所需并发。目标 GPU 还须容纳配置为 60% 的推理显存预算；进入训练阶段时，verl 会释放推理引擎占用的显存。

启动器通过 `SGLANG_LOGPROB_CHUNK_SIZE` 将 `--logprob-chunk-size`（默认 128）传给每个 Ray actor。Qwen3.5 的词表有 248320 个条目。按 2048 行分块时，单个 FP32 logprob 工作区约占 1.89 GiB，按行选择产生的副本可能同时占用显存。缩小分块可降低这部分临时显存，同时保留生成并发、回复长度和状态精度。GPU 能否容纳其他分配仍取决于实际配置。

如果模型检查点已保存，但最后一次评测失败，请保留原配方，同时指定 `--evaluate-checkpoint /data/mbpp-full/checkpoints/global_step_187` 和新的输出目录 `--out /data/mbpp-post-eval`。启动器会恢复原生检查点、同步权重，并设置 `trainer.val_only=true`，不再做梯度更新。单独保存失败记录，只将完整重跑的结果用于对比。verl 在验证结束后才写最后一个训练批次的日志，因此验证失败可能导致缺少该批次的转储，即使其优化器更新已完成、检查点也已保存。

替代后测完成后，运行以下命令，与原训练及其完整前测结果进行对照。

```bash
dsec-mbpp-compare --run /data/mbpp-full --post-run /data/mbpp-post-eval \
  --out /data/mbpp-full/comparison-recovered.json
```

从基础模型和新 LoRA 适配器开始训练。前后测都对相同的 500 道测试题各采样八次。原生 `val_files` 通道只用于留出集评测，测试分数不会参与梯度计算或检查点选择。评测安排在训练前和最后一步，中间检查点只保留一份 actor 状态。

对比工具核对训练集和测试集覆盖、生成与奖励之间的对应关系，以及 VM 回收情况。格式错误和候选程序超时产生的零分均保留。报告列出样本平均成功率（pass@1 估计值）、pass@8、从未解出变为解出及反向变化的题目、因长度结束的次数，以及按题配对的 pass@1 变化 bootstrap 区间。前后测按相同参数独立采样，所得区间只描述本次实验。评测行直接关联回执 ID。原生训练转储省略了自定义奖励字段，因此按题号、输出 SHA-256 和分数匹配训练行，保留重复次数，每张回执只使用一次。这种备用匹配方法无法区分输出完全相同的原生 uid。

<a id="selected-acceptance-preset"></a>

### 验收使用的配置

首次实验使用 **Qwen3.5-2B** 和 GRPO，**每题采样八条回复**。沿用此前的 LoRA 配置：rank 8、alpha 16，只训练语言模型的 MLP 投影层。Megatron 的 `linear_fc1`、`linear_fc2` 对应 HF/PEFT 的 `gate_proj`、`up_proj`、`down_proj`。验收训练前，检查实际可训练模块清单和 SGLang 适配器的目标层。

使用 **Non-Thinking** 模式，设置 `enable_thinking=false`，temperature 0.7、top-p 0.8、top-k 20、min-p 0.0、presence penalty 1.5、repetition penalty 1.0。固定版本的 verl 单轮循环会转发 temperature、top-p 和 top-k，设置 repetition penalty 为 1.0，但未转发 presence penalty。案例在该循环上显式转发 presence penalty 1.5 和 min-p 0.0，实测请求已确认这套配置生效。原生回复的 token ID、掩码及 logprob 均保留。

短程 GPU 试验已验证**整条回复最多生成 4,096 token**，同时设置 `data.max_response_length=4096` 和 `actor_rollout_ref.rollout.response_length=4096`，并通过 `data.apply_chat_template_kwargs.enable_thinking=false` 关闭 Thinking。启动器会同时设置两个上限和原生聊天模板选项。用 `--response-length` 和 `--generation-concurrency` 一并调整训练、评测的长度限制，实际取值记录于 `launch.json`。更换后端或版本后，请检查存档请求的实际参数和 token 数。记录 `finish_reason`、回复长度，以及代码或思考段不完整的比例。统计受长度约束的基线时保留触及上限的样本。因长度结束的回复若已包含完整代码仍可评分；输出不完整则按格式错误记零。保留训练框架原生 token ID 和掩码，分别记录服务故障和模型失败。

每题采样八次不限制沙盒并发为八。测系统吞吐时，各并发档使用相同的一组候选代码，每次执行新建沙盒。依次测试 8、16、32 并发，实测资源允许时再提高。另用多组提示测端到端路径，记录生成、沙盒执行、排队及总墙钟时间，报告执行吞吐、p50/p95 延迟、失败与超时数，以及 CPU、宿主内存、磁盘和网络峰值。分别标注冷创建和镜像或缓存已预热的条件。准入由调度器负责。负载测试中重复执行同一候选不能算作独立的 GRPO 训练样本。

要回放一组固定候选答案，请提供 JSONL，每条记录含 `solution_str`、准备好的 `ground_truth` 和原始 `expected_score`（0 或 1）。所有并发档使用同一批记录；负载检查通过意味着复现了原始判定，也包括原本就得零分的答案。

```sh
dsec-mbpp-verify --api-config "$DSEC_API_CONFIG" \
  --environment-id "$DSEC_MBPP_ENVIRONMENT_ID" --out /data/mbpp-generated-load \
  --candidates /data/fixed-candidates.jsonl --concurrency 8 16 32 48 \
  --host-interface YOUR_INTERFACE --disk-device YOUR_BLOCK_DEVICE
```

可选的资源采样每 0.25 秒读取一次 Linux 现有计数器。峰值涵盖整台宿主机及后台活动，无法将内存或磁盘 I/O 归属到单个沙盒。`--disk-device` 使用 `/sys/class/block` 中的设备名，例如 `nvme0n1`。提高并发前先配置实例容量和预算。固定样本在 32 并发时吞吐约为每秒 13 次执行，继续升至 48 并发时延迟增加。这次短测不足以确定整台机器的容量上限。

<a id="reward-and-evidence"></a>

## 奖励和证据

三条断言全部通过得 1 分；断言失败或候选程序运行错误得 0 分。候选子进程默认限时十秒，已明确处理的候选超时也得 0 分。外层验证器有独立的三十秒时限。结果消息缺失或截断、执行结果未知或服务故障时抛出异常，不能记为模型零分。

模型须返回一个完整的 Python 代码块。思考段未闭合或格式有歧义时，不执行代码，单独标记为 `model_format` 并记零。证据目录保存原始回复、源码与配置标识、执行结果、判定及清理或错误记录。传输失败时，先按已记录的 rollout ID 重新连接并核实，再决定后续操作，以免重复执行有副作用的代码。worker 持久记录执行动作，应用将解析后的判定写入宿主回执。该回执与 TB2.1 官方验证器在 worker 端的记录分开。此路径尚不支持持久恢复 verl 优化器会话。

验收依次检查以下项目：在真实 DSec MicroVM 中运行已知正确、错误和超时的样例；执行有明确预算的模型评测；验证每组 8 个样本相互独立；观察一次真正的 GRPO 更新，核对 token 和掩码；最后评测留出集。保留优势值为零的组，如实记录，不替换样本或修改奖励。带状态的多轮 Agent Loop 和跨框架 TB2.1 案例是此奖励路径通过后的独立工作，此适配器未验证这两项。

实际接入时遇到的问题及其验证证据见[首次使用报告](../../docs/reports/MBPP_VERL_FIRST_USE.zh-CN.md)。
