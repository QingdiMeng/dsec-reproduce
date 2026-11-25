<a id="openhands-on-the-dsec-api"></a>

# 通过 DSec API 运行 OpenHands

[English](README.md) · **简体中文**

<a id="current-integration-sdk-v1582"></a>

## 当前集成版本：SDK v1.58.2

当前控制器使用 **`openhands-sdk==1.58.2` 和 `openhands-tools==1.58.2`**。先安装核心 wheel，再在独立的 Python 3.12+ 环境中安装这个可选集成。

```sh
python -m pip install -c integrations/openhands/sdk-constraints.txt './integrations/openhands[sdk]'
```

`[sdk]` 和 `[legacy]` 分别提供不同世代的 `openhands` 命名空间，须安装在不同环境中。SDK 适配器为 `dsec_openhands.sdk`；下文的 Runtime 适配器保留用于重放早期实验。

官方 SDK 的 Agent/Conversation 管理提示、原生工具调用、历史、Thinking 事件和循环检测。官方 Terminal 和 FileEditor 在任务沙盒中执行。原生动作和观测经 DSec HTTPS API → Node/Edge → guest 回环 HTTP 传输。工具描述由 guest 提供，控制器工厂无法在本地构造任务执行器。guest 使用上游持久子进程/PTY 后端，显式设置软超时，并串行执行动作。撤销编辑仍由 guest 编辑器处理。SDK 遇到仅用换行分隔的多个命令，会返回原生错误观测；请使用文档所述的 `&&`/`;` 命令链或执行脚本。适配器不会改写模型命令以规避此约束。

`create_conversation` 要求新控制器进程及空的私有 HOME。本地工作区只存对话元数据，从上游继承的命令、文件传输和 Git 操作拒绝访问宿主。SWE-Gym CLI 在导入 SDK 前启动该进程，排除个人插件和钩子的干扰。模型凭据留在控制器中，不进入 guest 配置或工具请求。传输失败后禁止后续工具并中止 episode，未完成结果标为 UNKNOWN。中断有独立的原生会话及 HTTP 路由，避免被阻塞命令占有的 SDK 会话锁或 guest 动作锁挡住。固定版本的子进程后端通过 PTY 发送文档规定的 `C-c` 按键；上游的进程组信号有时会发给 shell 而非前台任务。回归要求命令提前以非零状态退出，仅收到中断回执不算通过。Agent 动作和观测仍由原官方执行器处理。调用方负责回收沙盒。持久化对话用于留存证据，目前无法自动恢复到另一沙盒或另一代工具进程。

工具描述中带有已发布的单次调用时限。超限请求在执行前收到可恢复的错误观测，无需静默调低时限，也不归为 UNKNOWN。真正的传输故障会封锁所有工具 worker，控制器在事件边界中止。worker 不会在控制器可能持有状态锁时调用 `pause()`。

通用工厂接受上游可选的 `condenser`。SWE-Gym 使用 `sdk_condenser.EvidenceSummarizingCondenser`，仅扩展固定版 SDK 摘要器的输入。触发阈值、原子事件边界和摘要插入位置遵循上游。摘要输入采用完整的原生事件 JSON，包括命令、编辑器参数、结构化 Thinking、工具输出末尾、调用 ID 及退出或错误状态，不使用 SDK 的 500 字符预览。每个分块及合并摘要都传入原始公开题面，因为上游保留的初始窗口不会自动成为摘要器的输入。超出预留 token 预算的输入分阶段分块处理；原始事件留在磁盘，生成日志保留所有摘要输入。摘要仍可能有模型错误。提示要求区分查看历史 diff 和实际应用补丁，并为修改及测试结果提供证据。摘要器不额外执行任务命令、修改源码或调整模型采样。

第三版支持上游在硬重置时逐步降低预览预算。预览字符串只用于估算目标 token 数，实际发送完整记录或记录片段；预算降低时随之缩小分块。空摘要立即使本次尝试失败，让上游尽早执行恢复，避免继续生成最终会被丢弃的分块。默认触发条件和采样参数沿用上游。第二版曾在预览限制降低后仍发出原来大小的请求，第三版修复了这一点。

harness 的身份检查、Git 清单及候选补丁采集通过同一沙盒中的 DSec 非 PTY 命令 API 执行。无需把终端控制序列解析成 JSON，也不改变模型看到的上游工具反馈。

用下文的 `prepare_tools.py` 发布可移植 Python 及 SDK/tools 1.58.2 依赖。在新的不可变环境中替换旧 ACI 工具层，保留 Linux `pip freeze` 与层摘要作为证据。episode 期间不安装依赖。控制器和 guest 均拒绝其他 SDK 版本。

对兼容 OpenAI 的模型别名，`sdk_llm.EndpointLLM` 可显式选择 `thinking_history='current_user_turn'`（Qwen 模板策略）或 `'all_assistant_turns'`（DeepSeek Thinking）。序列化时仅保留 SDK 结构化推理字段，Agent 历史和响应解析沿用上游。生成日志及事件用于评测，SAO 训练还需要精确 token 和 logprob 张量。本次未验证浏览器、宿主插件、图像查看、任意客户端工具或跨进程世代恢复。

可选 SWE-Gym 应用使用 `dsec-swegym-openhands` 调用新版 SDK，`dsec-swegym-openhands-legacy` 调用旧分支。固定任务和官方评分保持一致；新版 SDK 的结果须与原 Agent 基线分别记录。

<a id="optional-recovery-of-unparsed-minicpm-tool-markup"></a>

### 可选：未解析 MiniCPM 工具标记的恢复

`create_conversation(..., unparsed_tool_recovery='minicpm5', recovery_limit=3)` 启用固定版 SDK 的原生 Stop 钩子。若 assistant 正文包含未解析的 MiniCPM `<function name="...">` 标记，钩子会反馈本轮没有执行操作，并列出实际声明的工具。SDK 保留原回复，追加环境反馈，继续原对话。该过程不为未知工具起别名、不将文本解析为命令，也不执行回复文本。结构化调用仍由上游校验。正常作答及围栏或行内代码示例可正常结束。

默认最多反馈三次，支持配置为一至十次，仍受原轮数和 episode 时限约束。达到限制后，即使末次回复格式无效，也可能结束；此时不能据此宣称模型完成修复。SWE-Gym 将该 episode 标为 `unparsed_tool_call`，仍按官方评分。钩子执行失败会中止 episode，不能记成模型奖励。`tool-recovery.json` 和上游 HookExecutionEvents 记录尝试及钩子结果。默认关闭；基线、训练、后测必须保持一致。

钩子子进程只在专用控制器中运行固定的 Python 元数据读取程序。模型文本不会变成命令。任务工具仅在 DSec 沙盒内执行。这是可选的 Agent 集成策略，其检测范围限于所支持的模型调用语法。

<a id="experimental-exact-token-training-boundary"></a>

### 实验性精确 token 训练接口

可选的 SWE-Gym SAO 试验另在控制器回环地址运行 `python -m dsec_openhands.sglang_training`。它捕获 SGLang 原生输入/输出 token ID 与行为策略 logprob，对齐 SDK 响应 ID，并记录已发布的策略版本。不匹配时请求失败，不能将展示文本重新分词来代替。摘要请求单独记录并排除在策略目标外。每次生成保留自己的精确提示，包括当时使用的历史摘要。

此接口属于训练应用的本地推理环节。文件租约用于在生成与外部学习器之间协调单张 GPU，不提供分布式模型服务。DSec API 和 Agent 循环保持独立。与论文的对应关系、本地差异及验收范围见 [SAO 试验范围](../../apps/swegym/README.zh-CN.md#sao-integration-pilot-experimental)。仅凭通常的 SDK 生成和事件记录仍无法训练。

<a id="legacy-runtime-integration"></a>

## 旧版 Runtime 集成

此可选集成实现 OpenHands Runtime 接口。核心包不安装 OpenHands、基准任务、模型或训练器。`DSecRuntime` 中没有任务提示、评分器、模型响应解析器或 Agent 循环。其他 harness 也能复用它，但须提供已发布的编程环境。

参考 Agent 取自 SWE-Gym 的 OpenHands 分支 `e644a2ca45c3623b27a7e6c169e3d479f0a87fbc`（CodeActAgent 2.2）。[SWE-Gym 推荐此分支](https://github.com/SWE-Gym/SWE-Gym/blob/main/docs/OpenHands.md)。[SAO](https://arxiv.org/html/2607.07508) 提及 OpenHands，但没有注明提交号。此处的代码和依赖版本可以追溯，尚无法断言与作者使用的版本完全相同。

<a id="boundaries"></a>

## 职责边界

| 归属 | 职责 |
| --- | --- |
| 上游 OpenHands | 系统提示、CodeAct 工具与解析、历史、控制器及循环检测 |
| 本集成 | 动作和观测映射、稳定动作 ID、运行时能力、沙盒归属 |
| DSec | IAM 和准入、沙盒创建、原生会话/文件/HTTP、变更操作记录、隔离与回收 |
| Harness | 数据划分、公开题面、环境选择、补丁或制品收集及评分 |
| 外部模型与训练器 | 推理、模型配置、精确采样 token 与 logprob、RL 更新 |

正式路径为 OpenHands Runtime → DSec HTTPS API → Node/Edge → 沙盒。无 UDS 或控制器本地执行回退。基准 harness 无需自行解析 shell，也无需接管 OpenHands 对话历史。

<a id="install-the-controller-adapter"></a>

## 安装控制器适配器

先安装核心 wheel，再在独立 Python 3.12 环境中运行以下命令。

```sh
python -m pip install -c integrations/openhands/upstream-constraints.txt './integrations/openhands[legacy]'
```

上游早期依赖锁指定的 LiteLLM wheel 已无法从 PyPI 获得，此包改为固定到对应标签提交的官方源码。`e2b==0.17.1` 显式固定版本，因为自动范围解析会拒绝已撤回的旧版本；DSec 不使用该云服务商。版本约束来自所选分支的 poetry.lock。保存解析后的安装清单作为验收证据。依赖及其许可独立于本项目。任务镜像中无需安装整套控制器。

<a id="publish-the-coding-tools-separately"></a>

## 单独发布编程工具

预备沙盒需要在 `/opt/dsec-openhands` 下提供不可变工具目录，其中 `python/` 包含可移植的 Linux **Python 3.12**，`site-packages/` 包含 `guest-requirements.txt` 要求的依赖。在 episode 之外构建 wheelhouse 并固定全部制品，使用已有的无压缩 EROFS/DAX 层构建器发布该目录，加入环境的共享挂载。可移植解释器的原生库也应随工具制品分发。episode 启动和适配器均不下载依赖。任务命令继续使用镜像原有解释器；任务 shell 启动前移除工具 Python 的环境变量。

准备好这些目录后，源码发行包的发布器会将其写入根文件系统层中的 `opt/dsec-openhands/`。

```sh
python integrations/openhands/prepare_tools.py \
  --python-root /srv/build/portable-python-3.12 \
  --dependencies /srv/build/openhands-site-packages \
  --out /srv/dsec/layers/openhands-tools.erofs \
  --mkfs /usr/local/bin/mkfs.erofs
```

将返回的路径、哈希与大小记录在环境固定的 `readonly_layers` 中，准备 guest 模板时把 `/opt/dsec-openhands` 加入 `shared_mounts`。tar 存储从 guest 根目录开始的文件树，路径不相对于挂载点。单独发布一层不会修改运行中的 Edge 或可信目录。

工具进程提供实际执行能力：真实 PTY 执行命令，ipykernel 维护 Python 状态，原版 `openhands_aci.editor` 负责编辑和撤销。它只监听 guest 回环地址，通过 DSec 原生 HTTP 访问。无需开放宿主端口或安装新特权 helper。回收沙盒时一并回收工具进程和内核进程。运行时也按原样执行固定版本的上游 `INIT_COMMANDS`，包括 Git 身份和 `git --no-pager`。后台启动继承的信号状态与环境激活不得禁用 SIGINT 或破坏终端帧边界。每个新 bash 动作作为一份完整脚本在持久 shell 中执行；多行命令和 heredoc 只产生一个完成事件及最终退出码。行提示符不能将输出错配到后续动作。发给正在运行的前台进程的输入仍为原始 stdin。回归同时检查当前脚本结果与下一条命令的输出归属。

编程环境支持持久工作目录与环境变量、前台 stdin、非阻塞轮询、SIGINT、持久 IPython 与编辑器状态、有界文件传输、按行范围读取和目录列举。该环境未声明浏览器、VSCode 或递归归档传输能力。FnCall 每次调用后重置，无法承载持续编程工作区。首版适配器创建 Container 沙盒。

<a id="use-from-a-harness"></a>

## 从 harness 调用

创建上游 EventStream 和配置，将运行时传给上游控制器。运行时配置示例如下。

```python
runtime = DSecRuntime(
    config=openhands_config,
    event_stream=event_stream,
    sid=conversation_id,
    headless_mode=True,
    plugins=agent.sandbox_plugins,
    dsec_config={
        "api_config": "/run/dsec/client.json",
        "spec": {"environment_id": "published-coding-environment"},
        "resource_demand": {"cpu": 1, "memory_mb": 1280, "disk_mb": 2048, "network_mbps": 0},
        "cwd": "/workspace",
        "shell_init": "",  # explicit environment-specific initialization
        "command_timeout_ms": 45000,
        "transport_timeout_ms": 120000,
    },
)
await runtime.connect()
```

两项预算均须在运维人员发布的 guest 和环境目录上限内。HTTP 预算还覆盖内核启动和中断收尾，至少 35 秒，且至少比工具预算长 10 秒。须支持原生 HTTP。结果未知的调用不得重放。OpenHands 事件 ID 映射为稳定的 DSec 变更请求 ID；guest 工具进程检查负载身份和进程世代。工具进程重启后交互状态已丢失，必须报错。显式关闭运行时并检查回收证据。关闭时先阻止新调用，等待已提交 SDK 工作结束，再请求回收。取消上游等待者不会撤销已提交工作。只有在准入前被明确拒绝的 stop 可以原 ID 重试。重复关闭或由 atexit 关闭，均不能消除结果未知状态。

可选的 [SWE-Gym 应用](../../apps/swegym/README.zh-CN.md) 自行管理运行器，并在新沙盒中执行官方验证。冻结模型的生成日志可用于评测。精确训练记录仍由外部模型/训练器集成提供，不能直接把这些日志视为 SAO 的采样 token 和 logprob 张量。

<a id="verify-another-environment"></a>

## 验证其他环境

可选的 Linux 一致性测试独立于 SWE-Gym。将上述运行时配置保存为 JSON，指定新的证据目录；在装好控制器依赖的仓库环境中运行以下命令。

```sh
DSEC_OPENHANDS_ACCEPTANCE_CONFIG=/run/dsec/coding-runtime.json \
DSEC_OPENHANDS_ACCEPTANCE_OUT=/tmp/dsec-openhands-check-01 \
python -m unittest discover -s integrations/openhands/tests -p test_linux_runtime.py -v
```

测试会创建并回收一个沙盒，检查 shell/Python 状态持久性、stdin/轮询/中断、超时收尾、真实上游编辑器及撤销、原生文件范围读取、上游 Git 初始化、取消和回收顺序。报告须保留操作 ID 和清理证据。更换 Agent 版本、后端或环境时重跑，再独立验证基准的空补丁、参考解和模型实际得分。通过编程一致性检查无法证明浏览器、快照、FnCall、另一版本 Agent 或 RL token/logprob 的兼容性。

<a id="extending-the-integration"></a>

## 扩展集成

各接入点可独立调整。

| 变更对象 | 适配位置 | 应保留的检查 |
| --- | --- | --- |
| 其他 Agent | 可选的运行时、动作与观测适配器 | 执行语义、请求身份、取消和所有权 |
| 其他基准 harness | 对应的可选应用 | 公开任务准备、候选制品、独立官方评分 |
| 其他执行后端 | 已发布的 DSec 环境及能力声明 | 相同一致性测试、真实隔离和回收证据 |
| RL 训练器 | 外部训练器/应用连接器 | 精确采样 token、行为 logprob、掩码和策略版本 |

首版 OpenHands 适配器自行创建及持有沙盒。尚未实现连接到 harness 持有的沙盒；若扩展，需明确区分断开会话与停止他人持有的沙盒。无状态 FnCall 同样无法替代持久编程会话。按实际集成需要添加能力，并验证执行语义；单凭方法名或后端名称相同无法推断兼容。
