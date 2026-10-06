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

便携训练 recipe 仍在开发与验收；历史实验成功不能替代新入口验收。当前验证覆盖
少量任务与短训练，不证明 89 个任务全部通过、长上下文训练全部通过或模型学习
收益。单 rank NCCL 修复的上游草稿为
[Miles #3914](https://github.com/radixark/miles/pull/3914)。

## 接入其他训练框架

verl、Uni-Agent 可复用环境协议，但目前没有经过真实训练验证的专用适配器。新
适配器应负责框架的采样与轨迹格式，保留稳定 rollout/action ID，拒收无法证明
评分或 TITO 有效的样本。不要在任务插件里重新实现训练算法。

准备态分叉用于创建独立 episode；同一 episode 的暂停恢复用于继续原执行状态。
GRPO 同组样本必须使用独立可写状态和独立轨迹。不得把恢复旧 episode 当作一个
新的独立样本。
