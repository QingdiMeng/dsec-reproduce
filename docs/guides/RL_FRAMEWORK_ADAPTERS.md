# RL 框架接入

DSec 提供持久任务执行环境。训练框架负责推理服务、token/logprob、轨迹与优化器。
统一边界见 [Agent 环境接口](../architecture/AGENT_ENVIRONMENT_CONTRACT.md)。

## 已有 Miles 接入

安装核心的可选客户端依赖后，Miles 使用以下已安装模块：

```sh
python -m pip install '.[miles]'
```

| Miles 参数 | 入口 |
| --- | --- |
| `--custom-generate-function-path` | `dsec_adapters.miles_dsec_generate.generate` |
| `--custom-agent-function-path` | `dsec_adapters.miles_dsec_agent_function.run` |
| `--custom-rm-path` | `dsec_adapters.miles_dsec_generate.reward_func` |

这些参数只是接入点，不能代替完整的模型和训练配置。prompt 数据使用 Miles 的
消息列表格式，metadata 携带 `task_id` 与 `dsec_environment`。任务 instruction 在
环境 reset 时加入；不把解答或 verifier 内容放进策略 prompt。

设置 `DSEC_ROLLOUT_WORKER_SOCKET` 指向部署好的 worker。TB2.1 还需显式准备任务、
注册环境目录并配置 verifier 制品，参见 [可选应用](../../apps/tb21/README.md)。
安装核心不会下载任务或模型。

## 轨迹和奖励约束

agent 循环保存原始回复，解析动作后提交环境，并将观察接到多轮上下文。Miles
session server 记录 TITO；生成适配器核对 token/logprob/mask 与会话前缀。

仅可信任务判定可成为训练 reward。基础设施失败或缺失评分证据拒收；有效零分是
模型未完成任务。训练进程重启且丢失旧 TITO 时，即使沙箱恢复成功，也拒绝将该
episode 用于更新。

显式设置以下目录可保存作业证据：

- `DSEC_MODEL_OUTPUT_DIR`：原始模型回复。
- `DSEC_TITO_AUDIT_DIR`：逐轮 token/logprob 与请求记录。
- `DSEC_EPISODE_REGISTRY_DIR`：作业所属 episode ID，用于精确回收。

## 训练侧兼容修复的边界

短 Qwen3.5-4B Thinking GRPO 已在固定 Miles 镜像和单卡实验机上验证，记录见
[最新 GRPO 验收](../reports/DSEC_V01_GRPO_ACCEPTANCE.md)。使用了训练侧 decoder
释放、单 rank NCCL 生命周期、LoRA-only NVMe 和输出投影分块修复。这些修复不计入
DSec 系统功能，也不进入核心 wheel 或默认源码发行包。

独立训练 recipe 正在接受干净部署验收；历史实验成功不能替代新入口验收。当前验证
覆盖少量任务与短训练，不证明 89 个任务全部通过、长上下文训练全部通过或模型学习
收益。单 rank NCCL 修复的上游草稿为
[Miles #3914](https://github.com/radixark/miles/pull/3914)。

## Qwen3.5-4B × 全量 TB2.1 × GRPO

以下流程使用独立的可选训练制品，DSec 核心 wheel 和默认源码发行包仍不包含 GPU
内存补丁。Linux x86_64、单张 16 GiB NVIDIA GPU 是本轮验收配置；不是未经补丁的
Miles 显存需求保证。本流程从原始模型和全新 episode 开始，复用显式提供的只读
环境制品，不复用旧轨迹、ready VM 或训练 checkpoint。

### 1. 安装和准备任务

按 [Quickstart](QUICKSTART.md) 在新 venv 安装核心与构建工具，再安装可选应用：

```sh
.venv/bin/python -m pip install --no-build-isolation . ./apps/tb21
git clone https://github.com/harbor-framework/terminal-bench-2-1.git "$HOME/tb21-source"
git -C "$HOME/tb21-source" checkout 7131e4375048a0e408a8fb404b5f499d726b695b
.venv/bin/dsec-tb21 stage --repo "$HOME/tb21-source" --all --out "$HOME/tb21"
.venv/bin/dsec-tb21 check --suite "$HOME/tb21"
.venv/bin/dsec-tb21 register --suite "$HOME/tb21" \
  --catalog "$HOME/prepared-environments.json" --out "$HOME/tb21-catalog.json"
.venv/bin/python -m build --wheel --no-isolation --outdir dist/core .
```

这里必须提供 89 个已准备环境的 catalog、其只读层/根盘、VMM/kernel、verifier
磁盘和 manifest。注册不会创建这些制品。当前 `prepare-image` 的设备数限制和
GPU 任务限制仍然存在，不能把它理解为一条命令重建全部 89 个环境。

搬迁制品后，先更新 catalog 中所有绝对路径。OverlayBD 的 root descriptor 也包含
lower 路径；修改 descriptor 后要重新计算它的 `image_sha256`，保持实际 lower 和
EROFS 内容哈希不变。只复制 catalog 文件不足以完成迁移。3FS 是可选存储服务，
本流程可以使用本地只读层；模型和 trainer 镜像也是单独提供的依赖。

### 2. 部署独立的联网实例

按 [Host configuration](DSEC_HOST_CONFIGURATION.md) 配置一个新实例和全新 state
目录，设置已注册 catalog、全量 task suite、对应 verifier 和代理。任务专用
verifier/DAX 配置也应保留。调度预算要覆盖选中任务的最大单题资源需求，不能沿用
单个小任务的预算；`capacity` 和实际并发可以小于每题采样数量。

先配置 `network.max_slots` 和 `dns`，然后生成并由管理员安装权限。省略
`helper` 时，入口使用与 installer 一致的实例专用路径；也可以显式指定：

```sh
.venv/bin/dsec-host --config host.json render-privileges \
  --user "$USER" --slot-offset 6144 --out reviewed-privileges
sudo python3 reviewed-privileges/install-privileges.py --apply
```

地址段必须与本机其他实例不重叠。外部 OverlayBD/ublk 服务必须允许新 state
目录的写入，并允许访问制品目录；网络 installer 不会替代存储服务部署。
安装后使用生成的 `host-with-network.json`，按 Quickstart 渲染并启动两项用户
服务，再执行 `doctor --live`。不要在 helper 尚未配置或安装时执行联网启动检查。

正式模型训练前，用 `tools/verify_installed_task.py` 和已提供的固定轨迹检查一个
任务的创建、命令执行、官方评分与清理。这个已知答案 fixture 只用于独立验收，
不加入训练数据。完整训练仍使用官方 `tests/test.sh`，不能将 verifier 错误当成零分。

### 3. 获取独立训练 recipe

```sh
mkdir -p "$HOME/dsec-training/recipe"
gh release download v0.1.0-dev.0 --repo QingdiMeng/dsec-reproduce \
  --pattern qwen35-tb21-grpo-recipe-20261006-r4.tar.gz --dir "$HOME/dsec-training"
sha256sum "$HOME/dsec-training/qwen35-tb21-grpo-recipe-20261006-r4.tar.gz"
tar -xzf "$HOME/dsec-training/qwen35-tb21-grpo-recipe-20261006-r4.tar.gz" \
  -C "$HOME/dsec-training/recipe"
```

该制品的 SHA-256 为
`cbbfffe86604463097b448f29c5942d4f3973bba6813dbb48392d3dc727ccb79`。
其 `source-manifest.json` 固定每个源文件；包含 MIT/Apache-2.0 许可与 NOTICE。
私有项目下载需要有权限的 GitHub 登录。离线传输可以使用校验过的同一制品。

预先提供 Qwen3.5-4B 的完整 HF snapshot，并缓存此 trainer 镜像：
`radixark/miles@sha256:45ae7833f97a01369dbd8f336784830a87d94587bc010c7b8065291cc15cd7ca`。
镜像仓库镜像名可以不同，但 digest 必须相同；配置中填写本机实际存在的完整
RepoDigest。recipe 使用 `--pull never`，不临时下载或修改 trainer 镜像。

### 4. 配置、预检和训练

从制品内 `config.example.json` 创建自己的配置，替换七个绝对路径：`model`、
`core_wheel`、`suite`、`catalog`、`verifier_manifest`、`worker_socket`、`output_root`。
**删除示例中的单题 `tasks` 字段**，使任务选择来自全量 suite，并设置：

```json
{
  "groups": 89,
  "samples_per_prompt": 4,
  "concurrency": 2,
  "timeout": 172800
}
```

这只是需要合入完整配置的字段，不是独立配置文件。保留 `trainer_image` 时使用
本机实际 RepoDigest。一遍 89 题共 356 个 episode、89 个 GRPO 更新组；四条同题
样本使用独立沙箱和轨迹。`concurrency=2` 是同时运行的上限，不减少每组四条样本。
更多遍次将 `groups` 改为 89 的整数倍。模型服务在整个作业内复用。

```sh
.venv/bin/python "$HOME/dsec-training/recipe/recipe.py" plan --config full-tb21.json
.venv/bin/python "$HOME/dsec-training/recipe/recipe.py" doctor --config full-tb21.json
.venv/bin/python "$HOME/dsec-training/recipe/recipe.py" train --config full-tb21.json
```

确认 plan 中 `task_count=89`、`groups=89`、`episodes=356` 后才开始全量训练。
预检要求单张空闲 GPU、已运行的调度 worker、空闲端口和至少 40 GiB 可用磁盘。
训练使用 Megatron LoRA rank 8/alpha 16、MLP fc1/fc2、训练 microbatch 1，推理与
训练交替卸载。Thinking 由 `enable_thinking=true` 控制，采用 coding 采样配置
`temperature=0.6, top_p=0.95, top_k=20, min_p=0, presence_penalty=0,
repetition_penalty=1`；回复上限 32768，context 上限 65536。

agent 默认限制为 16 轮、单命令 120 秒、episode 1200 秒。可在配置中设置
`max_turns`、`command_timeout_ms`、`episode_timeout`；单命令最多 900000 毫秒，
且不能超过 episode 预算。大量依赖安装可能超过 120 秒；超时会终止命令，
后续 `sleep` 不会让已终止的安装继续。需要更长安装预算时，可在新作业中显式设置
`command_timeout_ms=600000`、`episode_timeout=3600`、`max_turns=32`。
这些是集成配置，任务官方 agent/verifier 时间分别见各自 `task.toml`，
不能据此宣称获得官方完整 benchmark 成绩。

### 5. 检查结果与失败

训练启动时打印 `run_directory`。每五秒更新 `progress.json`、
`resource-samples.csv` 和 `resource-summary.json`，不加载模型或 checkpoint。
用打印出的真实目录替换下面的 `/path/to/run`，可查询一次或连续观察：

```sh
.venv/bin/python "$HOME/dsec-training/recipe/recipe.py" status --run /path/to/run
watch -n 5 '.venv/bin/python "$HOME/dsec-training/recipe/recipe.py" status --run /path/to/run'
```

进度区分有效官方 verdict、正分、零分、中止/不确定 episode、完整题目覆盖和
已完成 optimizer step，并显示当前任务、命令/验证阶段、调度等待原因和
checkpoint 数。有效 verdict 不代表轨迹已成功进入训练；梯度是否非零单独报告。
`status` 标记超过十五秒未刷新的运行快照，失败退出保留最终状态和观测错误。

资源范围分别为整张 GPU、全逻辑 CPU 归一到 100% 的整机 CPU、训练容器 cgroup，
以及本作业 DSec VMM 的 PSS。容器内存不包含 VMM；VMM PSS 不包含共享存储服务。
GPU/CPU/PSS 峰值与阶段时间是采样估计；容器 `memory.peak` 来自内核。
IO 为训练容器 cgroup 的累计字节，网络为宿主默认网卡所有作业的累计字节，
磁盘记录剩余空间，均不能当作专属 sandbox 的磁盘/网络利用率。
共享 worker 的调度总量明确标为全实例范围，任务等待原因与已完成等待时间
按本作业 rollout ID 筛选。资源样本缺失不能作为零占用通过最终验收。

每次训练创建新的输出目录，保存配置、实际命令、原始模型回复、TITO、verifier
日志、资源采样、worker 记录与 LoRA checkpoint。基础设施错误会终止当前组并保留
证据，不能自动换题补齐数量。有效零分可以用于 GRPO；同组四条全零或全一时零
优势与零梯度是正常现象，不能把它解释成模型没有运行。

成功需要 `recipe-result.json` 的 `status=passed`，完整任务覆盖和有效轨迹检查
通过，并分别检查梯度与 adapter 变化。全量模式要求每题准确四条训练样本，不能
只看总计 356 条。`learning_update_observed` 只证明观察到更新，不证明学习收益。
容器正常退出或抛出异常时，将作业目录所有权还给启动训练的宿主用户，
确保原始回复与失败证据可直接读取；强制杀死容器仍可能需要管理员修复权限。
失败先保留该输出目录、定位并修复问题；本版本尚不提供一条命令自动断点续训。

## 接入其他训练框架

verl、Uni-Agent 可复用环境协议，但目前没有经过真实训练验证的专用适配器。新
适配器应负责框架的采样与轨迹格式，保留稳定 rollout/action ID，拒收无法证明
评分或 TITO 有效的样本。不要在任务插件里重新实现训练算法。

准备态分叉用于创建独立 episode；同一 episode 的暂停恢复用于继续原执行状态。
GRPO 同组样本必须使用独立可写状态和独立轨迹。不得把恢复旧 episode 当作一个
新的独立样本。
