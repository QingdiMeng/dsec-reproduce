<a id="use-cases-and-runnable-entry-points"></a>

# 应用案例与运行入口

[English](USE_CASES.md) · **简体中文**

DSec 负责沙盒执行、调度、存储和恢复。应用提供任务、Agent 和验证器。安装核心包不会安装基准测试、启动模型或选择 RL 算法。

下表列出已发布的入口、既有实验的验收范围，以及仍缺少的应用启动工具。

即使只部署一个节点，产品客户端也通过 `dsec.sdk.DSecClient` 访问需要认证的 API Server。直连 Edge 或 worker 的套接字仅供开发和节点内部使用。TB2.1/Miles 和 MBPP/verl 现使用 `RolloutAPIClient` 和 `DSEC_API_CONFIG`。历史训练得分来自早期的本机 worker 通信实验，迁移 API 后并未据此重新测得训练分数。请将 worker 套接字留在节点上。使用原生沙盒 API 的示例见[集群 SDK 指南](CLUSTER_DEPLOYMENT.zh-CN.md#remote-native-sdk)。

| 案例 | 已发布入口 | 验收范围 |
| --- | --- | --- |
| UC-01：TB2.1 任务执行与官方验证 | 可选包 `apps/tb21` 和固定轨迹验收工具 | 已验证任务准备和代表性任务的官方评分。已检查全部 89 道题的文件，尚未证明模型能在全部 89 道题上完成得分 episode。 |
| UC-02：通过 API 驱动 Agent，无需 GPU | 与框架无关的环境接口及 TB2.1 适配器 | 任务与环境接口已发布。评测整套任务时需自备模型客户端和 Agent 循环，独立的全量 API 评测 CLI 尚未提供。 |
| UC-03：Miles RL rollout | 包内的 Miles Agent、生成与奖励适配器 | 已通过 API 验证固定回复的 Miles 循环。真实短程 GRPO 证据来自早期本地通信。训练框架、模型和 GPU 补丁分别准备，历史全任务训练启动器尚未迁移。 |
| UC-04：从准备好的状态反复启动 episode | 基线封存与分叉 API、面向已安装系统的分叉验收工具 | 用 OverlayBD 案例验证状态隔离、真实快照恢复及共享对象清理。 |
| UC-05：TB2.1 以外的自定义任务 | 任务适配协议、计数器任务及 `dsec-host smoke` | 已验证非 TB 任务执行、评分、重启恢复及清理。 |
| UC-06：用 verl 在 MBPP 上做 GRPO | 可选的 `apps/mbpp` 数据准备、验证、原生训练及前后对比入口 | Qwen3.5-2B 完成全部 374 道训练题，每题八次采样，共 187 次更新。在 500 道题上完成前后测，样本平均成功率从 39.775% 升至 42.275%。 |
| UC-07：SWE-Gym 仓库任务 | 可选 SWE-Gym 应用与 OpenHands SDK v1.58.2 适配器，另保留旧版分支 | 通过真实 API 检查工具调用和取消语义，并完成官方评分。DeepSeek Flash 解出了验收中的 mypy 题。单题成绩不能推断整套解题率或 SAO 训练效果。 |

先按[部署快速开始](QUICKSTART.zh-CN.md)操作。以下命令假设核心包已装入当前 Linux 虚拟环境，且独立实例已准备好所需 guest、存储和网络。请将 `/data` 下的示例路径换成自己的路径。`tools/` 中的工具随源码包分发，在已安装的虚拟环境中通过 `python -I` 调用，避免从当前源码目录误加载运行时。每次验收使用新的输出文件名。

<a id="uc-01--tb21-task-execution-and-official-verification"></a>

## UC-01：执行 TB2.1 任务并运行官方验证器

使用本案例可在接入模型或 RL 前先验证 Agent 环境。需要时才安装可选应用。

```sh
python -m pip install ./apps/tb21
dsec-tb21 stage --repo /data/terminal-bench-2-1 \
  --tasks openssl-selfsigned-cert --out /data/tb21-case
dsec-tb21 check --suite /data/tb21-case
dsec-tb21 register --suite /data/tb21-case \
  --catalog /data/prepared-environments.json --out /data/tb21-catalog.json
```

任务仓库须检出固定修订 `7131e4375048a0e408a8fb404b5f499d726b695b`。注册命令读取已验证的环境目录，不构建磁盘。若尚无目录，请按[应用指南](../../apps/tb21/README.zh-CN.md)使用 `prepare-image`，并满足其本地镜像、工具和设备数要求。这两个命令都不会自动下载任务镜像。

按应用指南和[宿主配置](DSEC_HOST_CONFIGURATION.zh-CN.md)，为独立实例配置环境目录、准备好的任务和验证器磁盘。部署 [API、节点与 worker 桥接服务](CLUSTER_DEPLOYMENT.zh-CN.md#remote-native-sdk)，为 `default` 和选定的 TB2.1 环境分别配置最低资源需求。测试还会在默认计数器任务上验证 Miles 循环。向客户端提供私有 API 配置，将 worker 套接字留在节点。准备好后运行以下命令。

```sh
python -I /data/dsec-source/tools/verify_rollout_api.py \
  --api-config /run/dsec/api-client.json \
  --tb21-tasks /data/tb21-case/tasks --tb21-catalog /data/tb21-catalog.json \
  --tb21-trajectory /data/dsec-source/tools/fixtures/tb21-openssl.json \
  --out /data/evidence/tb21-api-fixed-trajectory
```

随附的测试输入包含已知正确的解答，用于验证执行链路。不能计为模型生成、模型质量证据或 RL 训练轨迹。工具执行已准备任务的官方验证器，保存判定和观测，并尝试回收沙盒和租约；错误记录在输出目录。worker 保存原始验证器日志。

`openssl-selfsigned-cert` 的验证器需要能下载依赖的网络，或该题完整的预制缓存。即使挂载共享工具盘，也应单独检查能否离线运行。API 迁移验收使用 `regex-log`、该题完整缓存及原始官方脚本，详见[验收记录](../reports/MBPP_VERL_FIRST_USE.zh-CN.md#api-migration-2026-10-12)。

仅做节点本地开发恢复检查时，可使用 `verify_installed_task.py` 保留的 `--config` 和直连 worker 入口。可选的 `--restart-services` 会重启专用实例的两个用户服务，检查运行中或已暂停沙盒的恢复及动作去重。此工具供运维验收使用，请勿向训练器暴露 worker 套接字。

<a id="uc-02--api-driven-agents-without-a-gpu"></a>

## UC-02：用模型 API 驱动 Agent，无需 GPU

在同一 TB2.1 应用中接入外部模型 API，可评测 Agent 质量，无需 GPU 训练。接入过程如下。

1. 根据已准备的任务和已注册目录构造 `TB2DSecEnvironment`，通过 API 客户端配置打开 `RolloutAPIClient.from_config`，再分配 rollout ID。
2. 创建 `DSecAgentEnvironment` 并调用 `reset(policy_prefix)`。任务适配器追加题目指令，返回的对话即模型的初始上下文。
3. 调用模型 API、解析回复，使用稳定的步骤 ID 和动作 ID 提交一次 `EnvironmentAction.shell`。将原始 assistant 消息作为 `policy_message` 传入，下一轮模型调用使用返回的对话。
4. 调用 `evaluate()` 获取官方判定，调用 `stop()` 释放沙盒。分别记录原始模型回复、模型配置、终止原因、命令耗时及验证器错误，并与任务分数对应。

动作结果未知时按原标识核实，避免重复执行。模型凭据、回复解析、停止规则和采样均由调用方负责。普通外部 API 的响应若要作为训练轨迹，还需有精确的 token、logprob、掩码和权重版本。

已安装的接口可用于自行集成；整合模型 API 客户端、回复解析和全量任务遍历的 CLI 尚未作为受支持示例导出。历史 harness 实验仍需其当时的驱动程序。随包提供的 `verify_rollout_api.py` 用固定回复验证官方评分，不调用外部模型或测量全量模型表现。详见[环境契约](../architecture/AGENT_ENVIRONMENT_CONTRACT.zh-CN.md)；应用驱动程序导出列入路线图 `RM-104` 的基准测试入口。

<a id="uc-03--miles-rl-rollouts"></a>

## UC-03：Miles RL rollout

DSec 提供任务环境及可信判定，Miles 负责生成会话、TITO 和参数更新。安装所需的可选 Python 依赖，配置沙盒连接。

```sh
python -m pip install '.[miles]'
export DSEC_API_CONFIG=/run/dsec/api-client.json
export DSEC_TB2_TASKS_DIR=/data/tb21-case/tasks
export DSEC_TB2_ENVIRONMENT_CATALOG=/data/tb21-catalog.json
```

将以下选项加入独立配置的 Miles 启动命令。

```text
--custom-agent-function-path dsec_adapters.miles_dsec_agent_function.run
--custom-generate-function-path dsec_adapters.miles_dsec_generate.generate
--custom-rm-path dsec_adapters.miles_dsec_generate.reward_func
```

任务元数据使用 `task_id` 和 `dsec_environment: "tb2"`。保留生成和奖励阶段的校验，缺少官方判定不能记为零分。训练会话若已丢失 token/logprob 历史，仅凭恢复后的对话文本无法继续训练。

最近验收使用 Thinking 模式的 Qwen3.5-4B，两个 GRPO 组，每组两条 rollout；回复上限 32768，上下文上限 65536。两组奖励分别为 `[1,0]` 和 `[0,0]`，第一组产生非零更新。这次实验验证集成流程，未证明学习收益。单张 16 GiB GPU 的历史实验依赖单独的训练端补丁；DSec 不安装这些补丁，未经修改的训练框架未必能在相同显存上运行。见 [RL 适配边界](RL_FRAMEWORK_ADAPTERS.md)和[最近一次训练验收](../reports/DSEC_V01_GRPO_ACCEPTANCE.md)。

模型下载和硬件准备由用户显式完成。可选 MBPP 应用提供固定版本的 SGLang/FSDP2 原生 verl 启动器，并已完成一个 epoch 及留出集评测。带状态的多轮 verl 和 Uni-Agent 适配仍在规划中。见 [MBPP 与 verl](../../apps/mbpp/README.zh-CN.md) 和[首次使用报告](../reports/MBPP_VERL_FIRST_USE.zh-CN.md)。

<a id="uc-04--independent-episodes-from-prepared-runtime-state"></a>

## UC-04：从同一运行态基线启动独立 episode

许多 episode 若都需执行相同的昂贵初始化（如启动服务或填充工作区），却要分别使用私有可写状态和新的 Agent 历史，可参考该案例。可信准备方封存基线；新 episode 引用不可变磁盘层，并恢复准备好的内存状态。

面向已安装系统的验收工具会验证内存中的 HTTP 计数器、各分支独立写入、全新历史、快照恢复及释放最后一个引用后的清理。

```sh
python -I /data/dsec-source/tools/verify_installed_fork.py \
  --config /data/tb21-host.json \
  --trajectory /data/dsec-source/tools/fixtures/tb21-openssl.json \
  --out /data/evidence/prepared-episodes.json
```

该配方需要已准备的 OverlayBD TB2.1 环境，以及能同时容纳基线和一个分支的容量。两个分支顺序验证，不能据此推算并发吞吐。`--restart-services` 增加独立实例上的重启恢复检查。测试输入仅用于 harness 验收。

基线不得包含参考答案或其他策略的历史记录。准备态内存分叉是本项目的扩展，与论文中仅处理磁盘的 `pack_diff` 流程有别。评估摊销时需计入基线创建成本、保留的内存和磁盘以及复用次数。见[环境契约](../architecture/AGENT_ENVIRONMENT_CONTRACT.zh-CN.md)和[成本报告](../reports/DSEC_VERITY_PILOT_REPORT.md)。

<a id="uc-05--custom-tasks-without-tb21"></a>

## UC-05：不依赖 TB2.1 的自定义任务

最快可运行的非 TB 示例是随包提供的计数器冒烟任务。

```sh
dsec-host --config /data/counter-host.json smoke \
  --out /data/evidence/counter.json
dsec-host --config /data/counter-host.json status
```

按快速开始构建新的非 TB guest 并启动此实例。无需 TB2.1 包、任务数据、模型或 GPU。测试使用已安装的 `CounterDSecEnvironment` 执行 shell 动作，核对精确返回值并检查回收。在专用实例上添加 `--restart-services`，可检查服务重启后运行中和已暂停沙盒的恢复。

自定义应用需实现 `TaskEnvironmentAdapter.prepare`、`step` 和 `evaluate`，并沿用 `DSecAgentEnvironment` 的生命周期。适配器定义指令、工作目录、资源需求及验证语义；Agent/模型循环由调用方管理。新任务适配器可以直接调用，接入 Miles 时还需显式注册插件并配置对应的判定校验。直接调用适配器不限于 TB2.1。

<a id="uc-06--qwen35-2b-grpo-on-mbpp-with-verl"></a>

## UC-06：使用 verl 在 MBPP 上训练 Qwen3.5-2B

按 [MBPP 应用指南](../../apps/mbpp/README.zh-CN.md)依次安装，准备固定版本的数据，构建通用 Python guest，配置独立实例，运行固定样例验收后再训练。可选包提供 `dsec-mbpp`、`dsec-mbpp-verify`、`dsec-mbpp-train` 和 `dsec-mbpp-compare`。核心包不安装此基准或 GPU 训练框架。

已完成的案例在一张 16 GiB RTX 4080 上，用 Non-Thinking 模式的 Qwen3.5-2B 和 LoRA 遍历全部 374 道原始训练题，每题采样八次，共 187 次 GRPO 更新。训练与评测保留原生 token、logprob 和掩码。DSec 在各自独立可写的无网络 VM 内运行生成的 Python 及三条原始测试。不同 episode 共享不可变 EROFS 层。这项工作验证执行奖励、调度、隔离与清理，未测试带状态的多轮交互或准备态内存分叉。

前后测对同样的 500 道留出题各采样八次。`dsec-mbpp-compare` 检查题目、样本覆盖和奖励证据，保留有效零分，报告样本成功率、八次内解出的题数、停止或格式错误及按题配对的 bootstrap 区间。本次样本成功率由 39.775% 升至 42.275%，八次内解题率由 64.8% 升至 68.4%。测试使用公开断言，结果来自单次采样实验，不能推断模型或沙盒具有普遍优势。

指南还说明如何限制 SGLang 的 logprob 工作区，以及如何从检查点仅重跑评测。单独保存失败证据，使用最终检查点重新获得一套完整后测，不增加梯度更新，不拼接不同轮次的部分结果。实际故障、恢复、留存证据及测量范围见[案例报告](../reports/MBPP_VERL_FIRST_USE.zh-CN.md)。

<a id="uc-07--swe-gym-repository-tasks"></a>

## UC-07：SWE-Gym 仓库任务

按 [SWE-Gym 应用指南](../../apps/swegym/README.zh-CN.md)准备固定版本的 SWE-Gym-Lite 子集并发布官方任务镜像，再通过公共 API 使用 `dsec-swegym-verify` 评测空补丁、官方参考补丁或自备候选补丁。一个 Container 的可写工作区支持连续检查仓库、编辑和验证。评测使用固定的上游脚本、解析器和评分器，完整日志、清理证据与数值奖励分别保存。`dsec-swegym-openhands` 配合可选的 [OpenHands SDK 适配器](../../integrations/openhands/README.zh-CN.md)运行 SDK v1.58.2 的官方 Agent/Conversation。旧分支单独提供 `dsec-swegym-openhands-legacy`。两者均沿用上游提示、解析和历史管理，通过 DSec 公共 API 在持续的工作区内交互，再在新沙盒中评测实际候选补丁。规范化的生成和事件日志用于冻结策略评测，并非 SAO 所需的原生训练张量。早期 `dsec-swegym-rollout` 工具另作 token 边界诊断。这两个入口均不实现或管理训练框架。

[首次使用记录](../reports/SWEGYM_FIRST_USE.zh-CN.md)区分了真实 harness 验收、有效的模型失败与单独开展的单题 SAO 训练试验。核心包不安装该数据集、应用、评测分支或训练框架。FnCall 的逐次重置语义无法提供持续的仓库工作区。这项验收未在三种执行路径上重复同一道仓库任务。

<a id="comparing-applications-on-docker-and-dsec"></a>

## 对比 Docker 与 DSec 上的应用

[对照协议](../architecture/DOCKER_DSEC_BENCHMARK_PROTOCOL.md)规定对齐任务、轨迹、缓存与存储条件和完整后端成本。[r5 成本报告](../reports/DSEC_VERITY_PILOT_REPORT.md)是历史试验记录。通用的应用扫描与成本评测启动器仍属路线图 `RM-104`。保留失败记录，分别描述功能验收、模型质量、RL 集成和系统性能。
