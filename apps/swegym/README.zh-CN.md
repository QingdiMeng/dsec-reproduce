<a id="swe-gym-through-the-dsec-api"></a>

# 通过 DSec API 运行 SWE-Gym

[English](README.md) · **简体中文**

当前 OpenHands 命令使用 SDK **v1.58.2**。在独立的 Python 3.12 或更新版本的控制器环境中先安装核心 wheel，再用一次安装命令解析两个可选包的依赖。

```sh
python -m pip install -c integrations/openhands/sdk-constraints.txt \
  './integrations/openhands[sdk]' './apps/swegym[harness]'
python -m pip check
```

一起解析依赖，可避免添加验证器依赖时破坏 browser-use 固定的 Click 版本或 LiteLLM 对 Hugging Face Hub 的版本约束。请发布 SDK/tools 1.58.2 的 guest 工具层；旧 ACI 层与此版本不兼容。详见[集成边界](../../integrations/openhands/README.zh-CN.md)。

```sh
dsec-swegym-openhands --task ./tasks/python__mypy-15413.json \
  --config ./sdk-case.json --out ./sdk-results
```

配置沿用已有 API、环境和 `openhands` 字段。请显式设置 `openhands.thinking_history`：Qwen Thinking 用 `current_user_turn`，DeepSeek Thinking 用 `all_assistant_turns`，也可用 `disabled` 关闭思考历史传递。采样参数同样需要显式设置。CLI 会先启动私有控制器进程，再导入 OpenHands。官方 Agent 只接收公开题面。回收沙盒前先保存候选补丁，官方验证器在新沙盒中运行。即使失败也保留 SDK 事件和生成日志。旧版入口 `dsec-swegym-openhands-legacy` 需在另一环境中安装集成包的 `[legacy]` 扩展。两个 Agent 版本的结果须分别标注。

自托管 SGLang 模型若使用别名，且端点支持 temperature/top-p，请显式配置 `openhands.capability_overrides: {"supports_sampling_params": true}`。这是上游 SDK 的能力覆盖字段，会原样传入；若缺失，SDK 的推理模型兼容逻辑可能省略已配置的采样值。如果 Thinking 完全由 `sampling_extra.chat_template_kwargs.enable_thinking` 控制，还可以在该端点设置 `supports_reasoning_effort: false`。请按端点分别配置，核对实际服务请求和最终生效的采样参数。

若 MiniCPM 端点把无法解析的 XML 工具调用留在 assistant 正文中，可在 `openhands` 中显式加入 `"unparsed_tool_recovery": "minicpm5", "recovery_limit": 3`。固定版本 SDK 的 Stop 钩子会在次数上限内反馈错误，并继续同一段对话；原始文本中的调用不会被改名或执行。冻结基线、训练和后测都应使用同一设置，其他模型格式无需开启。证据包括原始回复、下一次请求中的反馈、HookExecutionEvents 和 `controller/tool-recovery.json`。恢复不会重置轮次或时间预算。详见[集成契约](../../integrations/openhands/README.zh-CN.md#optional-recovery-of-unparsed-minicpm-tool-markup)。

`context_length` 是模型的**总上下文窗口**。运行器先预留 `response_length` 和 `context_safety_tokens`（后者默认 1,024），再将剩余输入预算交给 SDK 的官方 `LLMSummarizingCondenser`。总窗口为 65,536、回复上限为 32,768 时，输入可用 31,744 token，回复上限保持不变。对于本地 Qwen 端点，还需在控制器环境安装案例的 `[agent]` 扩展，把 `openhands.custom_tokenizer` 指向现有模型目录，例如 `/srv/models/Qwen3.5-2B`。此步骤只加载 tokenizer；请将其版本记入运行证据。官方摘要器会压缩发给模型的上下文，原始事件仍保留在磁盘。达到已识别的轮次或时间上限时仍捕获实际候选补丁并评分；模型传输故障或结果不确定的工具错误记为未评分。

这个可选应用准备固定版本的 SWE-Gym-Lite 训练题，在预先配置好的 DSec Container 沙盒里运行 SWE-Gym 官方评测脚本和评分器。使用该案例时再安装应用、数据集和 SWE-bench。Container 在连续命令间保留可写仓库；FnCall 每次调用后重置，无法提供本案例需要的持续工作区。

先用空补丁、官方参考补丁或自备补丁验证执行和评分。可选的 Qwen3.5/SGLang 工具另外记录真实的多轮模型轨迹。参考补丁通过说明评测链路正常，不能当作模型得分或训练结果。后文另述 SAO 训练接入，它需要单次 rollout、critic、采样时的 logprob 以及对 observation token 的掩码；仅运行异步 GRPO 不满足这些条件。

<a id="install-and-prepare-one-task"></a>

## 安装应用并准备一道题

先安装核心包，再在单独的应用环境中安装本包。

```sh
python -m pip install './apps/swegym[data,harness]'
curl -fL https://huggingface.co/datasets/SWE-Gym/SWE-Gym-Lite/resolve/main/data/train-00000-of-00001.parquet -o swegym-lite.parquet
dsec-swegym-prepare --source swegym-lite.parquet \
  --instance-id python__mypy-15413 --out ./swegym-tasks
```

准备工具会检查源文件的 SHA-256，并从 `prompts.jsonl` 中排除参考补丁和测试补丁。私有任务 JSON 包含评分数据和答案，不应放入模型上下文或 Agent 工作区。所用分支固定在 `242429c188fcfd06aad13fce9a54d450470bf0ac`，并校验评分器、解析器、常量及脚本生成器的源码。其他版本的 `swebench` 包会被拒绝。

<a id="prepare-the-environment"></a>

## 准备任务环境

运维人员先发布任务环境，通过常规 API 和节点配置声明最低资源需求。按[QEMU Container 宿主配置指南](../../docs/guides/DSEC_HOST_CONFIGURATION.zh-CN.md)操作。使用官方预构建实例镜像，保留基线提交和依赖，确保 UID 1000 对仓库和 pip 用户目录有写权限，尤其要检查 `/testbed/mypy.egg-info` 等生成的元数据。第一个镜像将 `HOME=/work`、`PIP_USER=1` 和 `/work/.local/bin` 加入 PATH。从已停止的官方镜像容器将原仓库复制到派生镜像，使用 `COPY --chown=1000:1000 testbed/ /testbed/` 设置文件归属。无需在物理宿主机上执行任务代码。保留现有 Conda 内容，准备前将镜像标签解析为不可变摘要。任务只使用 QEMU guest 内的 Docker，不要安装或开放宿主 Docker。

第一个案例使用 `xingyaoww/sweb.eval.x86_64.python_s_mypy-15413`，镜像仓库的拼写和数据集里的 `python__mypy-15413` 不同。联网与离线依赖需在环境侧配置，保留官方安装命令、测试命令及预期测试。用 `tools/build_docker_guest.py --storage-driver overlay2 --disk-mb 8192` 构建 guest，再使用 `startup_timeout_s: 240` 发布独立的环境目录条目。冷启动需导入大镜像；`vfs` 会复制各层的累积内容，耗尽 guest 的 8 GiB 磁盘。此配置仅作用于 guest 的存储路径，宿主 Docker 的存储驱动及工具命令时限沿用原配置。请在新的环境定义中选择 `disk_cache: direct`，避免导入产生的宿主脏文件页耗尽外层 VM 的 cgroup 额度。保持原有 guest 内存、VMM 额度和 IAM 最低需求，并在实际基盘文件系统上验证 Direct I/O 支持；遇到不支持的情况应明确报告。

<a id="move-shared-task-files-to-erofsdax"></a>

### 将共享任务文件迁移到 EROFS/DAX

若节点支持 DAX，运维人员可以在构建 guest 前拆分已准备好的不可变**本地镜像 ID**。这是可选的镜像发布步骤。应用与验证器继续通过同一公共 API 运行。

```sh
PYTHONPATH=src python tools/split_docker_image.py \
  --image sha256:YOUR_PREPARED_LOCAL_IMAGE_ID \
  --out /srv/dsec/prepared/mypy-15413 \
  --layer-root /srv/dsec/layers \
  --shared-dir /usr --shared-dir /opt/miniconda3 --shared-dir /testbed
python - <<'LAYERS'
import json
from pathlib import Path
root = Path('/srv/dsec/prepared/mypy-15413')
value = json.loads((root / 'split.json').read_text())
(root / 'layers.json').write_text(json.dumps(value['readonly_layers']))
print(value['task_archive_sha256'])
LAYERS
python tools/build_docker_guest.py \
  --base sha256:YOUR_PINNED_PYTHON_BASE \
  --docker-tools sha256:YOUR_PINNED_DOCKER_TOOLS \
  --source . --out /srv/dsec/templates/mypy-shared.ext4 --mode container \
  --task-archive /srv/dsec/prepared/mypy-15413/task-lean.tar \
  --task-archive-sha256 SHA256_PRINTED_ABOVE \
  --readonly-layers /srv/dsec/prepared/mypy-15413/layers.json \
  --shared-mount /usr --shared-mount /opt/miniconda3 --shared-mount /testbed \
  --storage-driver overlay2 --disk-mb 2048
```

按宿主指南创建发布根目录，并安装限于该目录的节点权限。新建环境条目，填入构建器生成的模板、内层镜像 ID、`readonly_layers` 和 `shared_mounts`，私有盘继续使用 `disk_cache: direct`。将 `case.json` 指向新条目，使用实测的磁盘需求下限，此 mypy 案例为 2,048 MiB。

拆分工具导出已停止的容器，在宿主机上无需执行任务代码。它保留从镜像读出的运行时 Config、数值 UID/GID、权限和 tar 元数据。精简后的镜像保留选定目录的挂载点，内容移至独立层，镜像 ID 随之改变。遇到跨分区硬链接、镜像声明的卷、缺失的根目录或根路径实际不是目录时，工具会拒绝处理。输入须为 Docker 已展平的导出文件，不能直接用它解析原始 OCI 层的 whiteout。层数不得超过节点的八层上限。

Container 中的文件编辑和生成的包元数据会写入各 VM 私有的 OverlayFS 上层。沿用原 harness 的 Git safe-directory 配置。不要为了通过探针而更改文件所有权或跳过安装、测试。发布后要重新验证 mypy 的空补丁基线和参考补丁。2 GiB 磁盘和选中的三个目录只适用于本案例。该路径尚未证明冷缓存按需读取的速度或整台 VM 的内存收益。

<a id="publish-baseline-and-ancestor-git-objects"></a>

### 发布仅包含基线及其祖先的 Git 对象库

部分官方镜像的 Git 历史中含有题目的后续修复。删除分支或重置 HEAD，仍可能留下已打包、不可达或 reflog 引用的对象。发布模型评测环境前，从已停止的镜像容器导出 `/testbed`，离线准备其 Git 元数据。

```sh
dsec-swegym-git-baseline --repository /srv/dsec/staging/testbed \
  --base-commit EXACT_TASK_BASE_COMMIT --out /srv/dsec/staging/baseline.git \
  > /srv/dsec/staging/repository-policy.json
```

新对象库包含原基线提交、**全部祖先**，以及从这些历史可达的所有 tree 和 blob。它保留提交身份和完整历史，排除后续及无关对象、远端配置、备用对象库、reflog 和额外引用。重新打包时，delta 的来源仅限于显式选定的对象集合。源镜像保持原样。在**新的**任务层或派生镜像中替换 `.git` 时，保留每个非 Git 文件及其数值所有权和权限。所有 guest 可见下层都应移除原 Git 库；仅使用 whiteout 或私有可写覆盖层，仍会把泄露对象留在原层。

将生成的 JSON 作为 `repository_baseline` 写入 `case.json`。OpenHands 运行器与官方验证器会在每个新沙盒中核对该策略，然后才展示题面或应用补丁。`repository-check.json` 和验证器报告保留实际对象集、提交集的摘要。发布前检查基线的 `git log`、`git show` 和 `git blame` 是否与源仓库一致。浅仓库或不完整的源仓库会被拒绝。发布后重跑空补丁和参考补丁，再在相同环境、新对话中比较模型。此项验收覆盖仓库历史泄露；模型预训练数据与互联网访问是另外的边界。

将常规客户端配置保存为 `api-client.json`。

```json
{
  "client": {"endpoints": ["https://api.example:8443"], "project": "research", "ca_file": "/run/dsec/ca.crt"},
  "token_file": "/run/dsec/token"
}
```

在 `case.json` 中指定已准备好的环境及**外层 VM** 的最低资源需求，仅填写内层容器内存不够。

```json
{
  "api_config": "/run/dsec/api-client.json",
  "spec": {"environment_id": "swegym-mypy-15413", "memory_limit_mb": 512, "cpu_cores_limit": 1},
  "resource_demand": {"cpu": 1, "memory_mb": 1280, "disk_mb": 8192, "network_mbps": 0}
}
```

<a id="check-scoring-before-connecting-a-model"></a>

## 接入模型前先验证评分

```sh
dsec-swegym-verify --task ./swegym-tasks/python__mypy-15413.json \
  --config case.json --mode baseline --out ./evidence/baseline
dsec-swegym-verify --task ./swegym-tasks/python__mypy-15413.json \
  --config case.json --mode oracle --out ./evidence/oracle
```

预期空补丁基线得 0 分，官方参考补丁得 1 分，两次都应记录 `cleanup=stopped`。每个 episode 新建沙盒。检查仓库、应用补丁和官方验证各有请求 ID、命令、输出和耗时，失败时也保留报告及完整测试日志。结果未知的操作不重放；缺失或不完整的验证不能算有效零分。若官方日志超过常规的 8 MiB 传输上限，例如 `git show` 展开浅仓库的根提交，运行器会压缩传输，再把完整解压日志交给同一官方解析器。验证脚本不截短。压缩数据上限为 8 MiB，解压后上限为 128 MiB，超出则记为未评分。单命令默认限时 30 秒。在 `case.json` 中分别设置工具和准备命令的 `command_timeout_ms` 及官方验证的 `verifier_timeout_ms`，两者都不能超过运维人员固定的环境上限（最高 300,000 ms）。支持更长命令的 guest 构建方法见宿主指南。验证器超时或未完成时保留已有日志，结果记为未评分。

对支持长命令的新环境，在案例配置中加入以下设置，单位为毫秒。

```json
{"command_timeout_ms": 45000, "verifier_timeout_ms": 120000}
```

运维人员准备的 guest 与执行目录都必须允许至少 120,000 ms。只改案例配置无法延长旧 guest 的命令时限。命令执行时仍可查询状态。调用方断开连接或取消后，应按原请求 ID 查询；不能假定执行已停止，也不应重发结果未知的动作。API 重启、超时与资源等待的有限验收见首次使用记录的训练前检查部分。

评测自备补丁时使用 `--mode patch --patch solution.diff`。在新沙盒中运行官方评测，以排除模型 episode 遗留的任意工作区状态。这里沿用上游解析器和评分器。仅凭任务输出的日志，无法防止恶意候选程序伪造测试输出。

实测结果及待验证的集成工作见[首次使用记录](../../docs/reports/SWEGYM_FIRST_USE.zh-CN.md)。运行验证不会启动模型、训练框架或 SAO 流程。

<a id="legacy-codeactagent-baseline"></a>

## 旧版 CodeActAgent 基线

在应用环境中安装独立的[OpenHands 集成](../../integrations/openhands/README.zh-CN.md)，即可运行编程 Agent 基线。适配器保留固定版本的上游 CodeActAgent 提示、解析器、历史和控制器。DSec 提供执行环境，应用负责公开题面、候选补丁及新沙盒中的官方验证。该集成独立于 SWE-Gym，可复用于其他评测框架。

使用新的环境和模板，在任务层旁将集成提供的不可变 Python 和编辑器工具层发布到 `/opt/dsec-openhands`。保留任务原有解释器和依赖。扩展 `case.json` 如下。

```json
{
  "openhands": {
    "model": "openai/dsec-swegym-qwen35-2b",
    "base_url": "http://127.0.0.1:31026/v1",
    "native_tool_calling": true,
    "temperature": 0.6,
    "top_p": 0.95,
    "sampling_extra": {"top_k": 20, "min_p": 0.0, "presence_penalty": 0.0, "repetition_penalty": 1.0, "chat_template_kwargs": {"enable_thinking": true}},
    "response_length": 32768,
    "context_length": 65536,
    "model_timeout_seconds": 600,
    "max_iterations": 100,
    "episode_seconds": 1200,
    "runtime": {
      "cwd": "/testbed",
      "shell_init": "source /opt/miniconda3/bin/activate && conda activate testbed",
      "transport_timeout_ms": 120000
    }
  }
}
```

保留外围 API、执行规格、资源需求、工具和验证器预算。端点必须实际支持 OpenAI 工具调用；对于 Qwen3.5/SGLang，请配置工具及推理解析器。显式的能力声明可覆盖旧分支的模型名称白名单，仍使用原动作解析器。API 凭据可通过 `openhands.api_key_env` 提供，请勿写入任务提示。以下命令不启动模型。

```sh
dsec-swegym-openhands-legacy --task ./swegym-tasks/python__mypy-15413.json \
  --config case.json --out ./evidence/openhands-episode
```

报告保留上游源码标识、生成封装器发送的请求、LiteLLM `ModelResponse` 对象、OpenHands 事件与历史、DSec 操作 ID、候选补丁及新沙盒的官方验证结果。这些是冻结策略评测记录。SAO 训练还需要精确的采样 token 和 logprob 张量。本地试验的上限为 100 轮、65,536 token。SAO 论文报告的 OpenHands 配置为 300 轮、128k 上下文，未给出精确源码提交。比较结果时应注明这项差异，并将模型解题表现与基线、参考解的链路验收分别报告。

对上述兼容 OpenAI 的原生 Qwen Thinking 配置，运行器也在 `completions/provider/` 中保存实际发送给提供方的请求正文和响应正文，不含 HTTP 头。旧 LiteLLM 转换器会丢弃 `reasoning_content`，此版本的 Agent 也未在历史中保留该字段。每个 episode 的模型适配层按准确的工具调用标识，为当前用户与工具交互链恢复该字段，符合[Qwen 模板](https://huggingface.co/Qwen/Qwen3.5-2B/blob/main/chat_template.jinja)；已完成的早期用户轮次不补入。工具名、参数、输出、系统和任务指令以及上游 Agent、解析器和循环检测均沿用原行为。无法匹配的历史会被拒绝，不能在缺少提供方记录时恢复旧 episode。原始 JSON 正文仍不足以满足精确采样 token 和 logprob 的训练要求。

使用 DeepSeek Thinking 时，只替换端点和模型相关选项。由凭据管理器提供所需的环境变量。

```json
{
  "model": "openai/deepseek-flash",
  "base_url": "https://api.deepseek.com",
  "api_key_env": "DEEPSEEK_API_KEY",
  "native_tool_calling": true,
  "sampling_extra": {
    "thinking": {"type": "enabled"},
    "reasoning_effort": "high"
  }
}
```

[DeepSeek 工具调用和 Thinking 协议](https://api-docs.deepseek.com/guides/thinking_mode/)要求保留**所有**历史 assistant 轮次的推理，包括没有调用工具的回复。此配置按原顺序还原回复，将 assistant 文本片段序列化为 API 需要的字符串，保留原文及工具调用。历史缺失或发生更改时会拒绝继续。不要转发 Qwen 专有的 `top_k`、`min_p`、`repetition_penalty` 或 `chat_template_kwargs`。DeepSeek 在 Thinking 模式下忽略 temperature，top-p 的行为由模型确定。请记录提供方实际生效的约定，同一组配置值不代表不同模型族使用了相同的采样。运行器会保存实际返回的模型标识及完整请求和响应正文。做模型对照时，固定任务、工具定义、环境、验证器，以及交互次数、时间和输出上限。

固定版本的控制器若以已确认的 `Agent got stuck in a loop` 结束，应保留该策略终止原因，捕获实际候选补丁，另起沙盒正式评分。解析错误只有在完整的模型回复确实包含未知工具或无效 JSON 参数时，才按同类情况处理。被拒绝的调用不改名、不修补、不执行，也不据此虚构分数。未匹配的 ERROR、提供方故障、执行结果未知或验证证据不全，均记为未评分。独立于 OpenHands 自身的重试设置，提供方客户端的重试已显式关闭。模型请求时限应与输出上限匹配，另行设置命令和 episode 的时限。

<a id="sao-integration-pilot-experimental"></a>

## 实验性 SAO 训练

`dsec-swegym-train-sao` 基于固定版本的 verl 原生 FSDP2 引擎，在小显存条件下实现了 [SAO 论文](https://arxiv.org/html/2607.07508)中的训练机制。这是本项目的实现，实验配置与论文不同。2026-10-28，单题、两次更新的训练闭环在 Linux 上跑通。四个 episode 都在 20 轮上限内得到 0 分，该实验未证明学习效果提升。

每次准入只采样一个 rollout。critic 对每个动作 token 预测价值，每更新两次 critic 才更新一次 actor。计算 GAE 时跳过 observation，直接重要性采样使用 rollout 引擎在真实采样时记录的 logprob。代码任务中 DIS 采用开区间 `(0.2, 4)`。actor 和 critic 的学习率分别为 `1e-6` 和 `5e-6`，critic 的注意力参数保持冻结。

本次实验调整了以下配置：

| 设置项 | 论文配置 | 本地集成试验 |
| --- | --- | --- |
| 编程模型 | Qwen3-30B-A3B-Thinking-2507 | MiniCPM5-2B-SFT |
| 编程评测 | SWE-Bench Verified | 一道 SWE-Gym 题，未做留出题评测 |
| 更新范围 | 冻结注意力的 MoE critic | actor 与 critic 都更新稠密 MLP 的 LoRA，另训练标量价值头 |
| critic 初始化 | 强调价值模型预训练 | SFT 主干加新初始化的价值头，尚未进行价值模型预训练 |
| 资源 | 异步 RL 训练环境 | 推理和训练共享一张 16 GiB GPU，沙盒工具可以重叠执行 |
| 训练批次 | 128，组大小为 1 | 每次更新使用一个已完成的 episode，组大小为 1 |
| critic 预热 | 10 步 | 此次两次更新的生命周期试验未做预热 |
| 编程任务预算 | 300 轮，128k 上下文 | 100 轮，65,536 token 上下文，32,768 token 回复；首轮生命周期试验使用 20 轮 |

actor 和 critic 的 LoRA 适配器都使用 rank 8、alpha 16，目标层为 `gate_proj`、`up_proj`、`down_proj`。这是本地配置选择。单题、两次更新的实验验证训练生命周期与参数发布，无法据此判断学习效果或复现论文分数。

本地 SGLang 采集网关保存每次请求的原生提示 ID、实际采样输出 ID、行为策略 logprob 及已发布的策略版本。记录不匹配时拒绝接收。OpenHands 可能重新序列化或压缩历史，因此每次生成均按其实际上下文参与训练。辅助摘要只作为条件输入，不作为策略学习目标。普通评测保留的 JSON 正文不够。学习器逐条接收已评分的 episode；执行更新时，共享 GPU 租约暂停推理。导出的 LoRA 发布后，才接纳下一次生成。

以上训练逻辑位于可选应用和集成包中。DSec 负责沙盒的调度、执行、隔离和生命周期。actor/critic 参数、模型卸载及 RL 算法归训练框架管理。本原型依赖 verl 修订 `8718ca30a3f002f93b7c4fd99b9b2506718681bc` 的内部实现，尚不能作为通用分布式训练适配器使用。冻结输出头的 Triton 桥接代码只支持单 rank。

请按固定版本单独准备训练环境，在其中安装两个可选的应用和集成 wheel。控制器环境另需安装 SDK 集成和应用。先发布任务环境并准备常规 DSec API 案例配置。模型目录须包含所选权重及配套的 tokenizer 和模板。确认控制器回环端口 31026、31027 均空闲，再执行以下命令。

```sh
dsec-swegym-train-sao --task ./swegym-tasks/python__mypy-15413.json \
  --case-config ./case.json --model /models/MiniCPM5-2B-SFT-controlled \
  --sdk-python /venvs/openhands/bin/python \
  --trainer-python /venvs/pinned-verl/bin/python \
  --out ./evidence/sao-pilot --steps 2 --max-iterations 100
```

使用新的输出目录。此命令执行前测、后测，以及同一道题的多次独立训练 episode，保留精确生成记录、官方评分和 LoRA/优化器检查点。它不选择数据集划分，也未实现从检查点续训。这次验收有意在同一 GPU 上串行生成和更新，没有验证异步吞吐收益或不同策略版本混合轨迹的校正效果。实测数据及限制见[首次使用报告](../../docs/reports/SWEGYM_FIRST_USE.zh-CN.md)。

评测沿用案例的 episode 和单次模型请求时限，原案例分别为 1,200 秒和 600 秒。训练 episode 设为 7,200 秒，为单卡交接队列留出时间。训练与评测的时限分别记录，GPU 等待区间保存在 `gpu-timeline.jsonl`。

恢复 100 轮上限前，先取早期成功 episode 中最长的三段 Agent 上下文进行容量测试。常驻 SGLang 释放并恢复显存，试算所得梯度全部丢弃。最长段为 26,595 token，原生训练分配峰值为 12,068.38 MiB，整卡采样峰值为 15,133 MiB。这只验证了上述上下文，不能保证任意 65k token 的段都能训练。历史 rollout 缺少行为策略 logprob，不可作为 SAO 训练数据。诊断模块 `dsec_swegym_case.sao_memory_probe` 使用显式诊断损失，不保留任何更新。真正的 RL 从 SFT 权重和新采集的行为概率开始。

整条变长轨迹暴露了三段试算未覆盖的显存分配碎片。学习器现会在 CUDA 初始化前设置 `PYTORCH_ALLOC_CONF=expandable_segments:True`，SGLang 沿用自己的分配器配置。随后包含 56 段和 97 段原生生成记录的完整更新均通过，最长段为 31,741 token。训练完成两次 actor 更新和四次 critic 更新。首次后测因原生日志按小时轮换导致采集失败，未取得有效评分。修复读取程序后单独重跑后测，奖励为零。模型最终回复调用了不可用的 `execute_bash` 而非 `terminal`，未提交源码修复。这验证了训练执行流程；尚无证据表明训练提升了效果。原始记录及限制见首次使用报告。

<a id="diagnose-and-warm-up-the-critic-before-another-policy-run"></a>

### 诊断并预热 critic

`python -m dsec_swegym_case.critic_warmup` 仅运行可选的 verl 原生 critic。它加载已保存的 token 分类适配器及**标量头**，验证其状态摘要，全程无需构建 actor。清单须包含 `training`（训练 episode 目录列表）和 `reference`（单独的参考 episode 目录）。各目录需有运行器的 `case/` 证据以及精确的原生 `generations/` 采集记录。episode 之间的交互、采样、仓库和时限设置必须一致，均使用冻结 SFT 策略，完成官方评分及沙盒回收。基础设施故障不能作为零分标签。摘要生成始终排除在训练目标之外。

当前配方中 γ 和 critic 的 λ 均为 1，因此每个动作 token 的蒙特卡洛训练目标都等于该 episode 实际得到的终局奖励。回报为常数时，解释方差无定义，报告记为 `null`。同时报告 MSE、RMSE、MAE、偏差和预测值范围，保留每个 token 的预测。计算这些指标时沿用价值头的原始输出，不做裁剪。

```sh
PYTORCH_ALLOC_CONF=expandable_segments:True /venvs/pinned-verl/bin/python \
  -m dsec_swegym_case.critic_warmup \
  --model /models/MiniCPM5-2B-SFT-controlled --adapter ./saved-critic \
  --manifest ./warmup-manifest.json --out ./evidence/critic-warmup \
  --steps 10 --lora-lr 0.000005 --head-lr 0.0001
```

先停止推理服务，释放单张 GPU。输出目录须为新目录。预热共做十次优化器更新，每一步从每个训练 episode 中均匀选取两次生成，保留完整上下文。前后诊断固定使用训练轨迹首、中、末三处生成，并检查未参与训练的参考 episode 中的**每一次**生成。标量头使用较高学习率是本地预热设置，与论文的在线 critic 学习率不同。主干和注意力参数保持冻结，只更新 MLP LoRA 和标量头。报告分别记录流程成功、预测误差下降及混合奖励小样本验收。全为正奖励的数据无法通过这项验收。单题参考 episode 不能替代留出题评测，命令不会自动把 critic 投入 actor 训练。

冷启动时将标量投影权重清零，并使用先验 `0.5`，也可显式指定 `dsec-swegym-train-sao --critic-value-prior VALUE`。初始化在加载主干和 PEFT 模块后、创建 FSDP 和优化器之前执行，依据[JustRL-II](https://github.com/merak0514/JustRL-II/blob/75814318f243af7e39af337c1173fc3927c9635b/miles/backends/megatron_utils/model_provider.py)的初始化原则。该项目数学任务中的 `0.52` 不是通用先验。这里的原生 HF token 分类器不使用 Megatron 中与 LM 输出头重叠的检查点加载路径，没有证据显示同类权重污染。常规 `--critic-adapter` 会精确恢复保存的输出头，同时指定冷启动先验会被拒绝。

要单独比较**输出头初始化方式**，请为仅预热价值模型的命令添加 `--reset-value-head-from-training-mean`。工具先校验已保存的完整头部和 LoRA 状态，保持 MLP 权重，再仅将输出头重置为零权重，并以**训练 episode** 的不加权平均奖励作为初值，排除参考集标签。这是一次新的初始化；默认预热会保留已有输出头。四个 episode 的奖励 `1,1,0,1` 在本试验中给出先验 `0.75`，单题四个示例不足以可靠估计编程 Agent 的全局先验。初始常数预测不区分状态，初始 MSE 较低也不能证明模型学到了区分能力。若所有回报相同且头部预测已与其一致，零梯度是合理结果，应与原因不明的参数未更新分别记录。JustRL-II 的 30 批纯 critic rollout、此处的十次优化器更新及 SAO 论文的价值模型准备是三种不同设置。

审查证据后，可用 `dsec-swegym-train-sao --critic-adapter PATH` 从导出目录初始化新试验的 critic。它会核对恢复的输出头和 LoRA，并创建**新的优化器**。这是权重初始化，不是中断续训。在线 critic 学习率保持 `5e-6`。这些训练功能均在可选应用中实现。

<a id="validate-a-prepared-critic-on-independent-episodes"></a>

### 在独立 episode 上验证已准备的 critic

`python -m dsec_swegym_case.critic_validate` 评估恰好四个新取得官方评分的冻结 SFT episode。清单用 `validation` 列出四个目录，用 `--prepared-from` 指向此前的预热清单。验证 episode 的路径和原生回复标识都不得与预热训练集、参考集重叠，交互协议须一致。

```sh
PYTORCH_ALLOC_CONF=expandable_segments:True /venvs/pinned-verl/bin/python \
  -m dsec_swegym_case.critic_validate \
  --model /models/MiniCPM5-2B-SFT-controlled --adapter ./prepared-critic \
  --manifest ./validation-manifest.json --prepared-from ./warmup-manifest.json \
  --prior 0.75 --out ./evidence/critic-validation
```

先停止推理服务。这条命令无需构建 actor、优化器或学习率调度器，恢复的输出头及 LoRA 状态摘要必须保持一致。对每条动作 token，按原生完整上下文计算并保存预测。主指标将四个 episode 的 MSE 等权平均，同时报告按 token 混合计算的次要指标。先验须在验证前按训练数据固定，不能根据新评分挑选。以上的 `0.75` 只适用于这次四样本试验。

报告分别记录流程状态 `status` 和验收结果 `gate_passed`。验收要求样本中既有 0 分也有 1 分，且候选 critic 的 MSE 为有限值并不高于常数先验的 MSE。未通过时实验停止，不会自动增加 episode 或预热步数。通过只说明模型通过了这一个任务上的小规模筛查。

有条件地启动训练时，除 `--critic-adapter ./prepared-critic` 外，传入 `--eval-episodes 4` 和 `--before-validation ./evidence/critic-validation/report.json`。若验收未通过，或检查点、清单、episode 证据、交互协议已改变，运行器会在启动训练前拒绝。它复用四个已验证的评分作为前测，训练结束后再独立采集四次后测。现有调用方的默认评测次数仍为一次。每次更新另存 `learning-signals.json`，记录原生回复标识、精确的 critic 目标、更新前后预测值和实际 actor 优势值；结果中含该文件的哈希。

<a id="legacy-diagnostic-scaffold"></a>

## 早期诊断工具

以下是早于 OpenHands 集成的本地循环，保留用于诊断 token 边界。编程 Agent 基线优先使用前文的 OpenHands。

在应用虚拟环境中安装可选 tokenizer 和诊断工具依赖。

```sh
python -m pip install './apps/swegym[agent,harness]'
```

单独部署 SGLang 原生 `/generate` 端点，使用与本地相同的 Qwen3.5 tokenizer 和权重。模型的安装、启动、卸载和更新均在 DSec 核心之外完成。首轮试验在 16 GiB GPU 上使用现有 Qwen3.5-2B 检查点。此工具按该检查点的 XML 函数调用模板解析；其他模型的调用格式需使用相应的策略或适配器。

```sh
dsec-swegym-rollout --task ./swegym-tasks/python__mypy-15413.json \
  --config case.json --model /models/Qwen3.5-2B \
  --endpoint http://127.0.0.1:31026 --policy-version CHECKPOINT_CONTENT_DIGEST \
  --max-steps 30 --episode-seconds 1200 \
  --response-length 32768 --context-length 65536 --out ./evidence/model-episode
```

编程任务默认使用 Thinking 模式，temperature 0.6、top_p 0.95、top_k 20、min_p 0、presence_penalty 0、repetition_penalty 1。轮次和时间预算是本工具的试验配置，不是 SWE-Gym 的官方要求。模型只能看到题面和已声明工具，不能看到参考补丁或隐藏测试补丁。命令及反馈均经过常规 DSec API。每条模型命令在独立 shell 中激活已准备的 `testbed` Conda 环境，采样前先检查 Python 前缀及依赖。同一回复内的调用须全部验证通过才会按顺序执行。格式错误时整批不执行，并返回格式反馈。每条命令使用显式案例预算，默认为 30 秒。

每轮保存原生请求（含提示 ID）、未经改写的响应、采样 ID、logprob 及结束原因。`trajectory.json` 追加实际采样 ID 时将损失掩码设为 1，追加模板渲染的观测时设为 0。前轮已生成的 ID 直接进入后续提示，不会先解码再分词。上下文耗尽时结束 episode，历史不会被静默裁剪。截断回复中的不完整工具调用不会执行。报告保存已采集的完整命令输出，传给模型的反馈则采用 DSec 有界 shell 观测格式，分别记录退出状态、采集截断和上下文截断。

正常结束或触及步数、时间、token 上限时，捕获当前仓库差异、停止 Agent 沙盒，再将补丁交给新建的官方验证沙盒。采集涵盖已跟踪改动、新普通文件和符号链接，也支持文件名含 Git pathspec 特殊字符。未跟踪的嵌套仓库（含未完成的 clone）不属于父仓库改动，会排除并列入 `capture_candidate` 输出。忽略文件同样排除。Git 出错时记录证据并让采集失败，不替换为空补丁或零分。未完整结束的 episode 也保留其实际测试分数。执行结果未知或基础设施失败时保留证据、记为未评分，不能以新动作重放或换算为有效零分。

`--policy-version` 是运维人员提供的冻结检查点内容标识。若原生引擎提供权重版本元数据，记录中也会保留。本工具尚未实现权重热更新或跨版本训练。屏蔽观测 token 只是 SAO 的一个条件，外部训练器仍需实现跳过观测的 GAE、critic 更新频率和 DIS，并执行真实的训练前后评测。
