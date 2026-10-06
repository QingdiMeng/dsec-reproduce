# Agent 环境接口

`AgentEnvironment` 是任务与训练框架之间的边界，定义在
[agent_environment.py](../../agent_environment.py)。它不替代沙箱 SDK，也不依赖
OpenEnv 服务。

## 职责

| 层 | 负责的内容 |
| --- | --- |
| 训练框架的 agent loop | 模型采样、回复解析、多轮消息、token/logprob、RL 算法 |
| `TaskEnvironmentAdapter` | instruction、环境与资源选择、动作执行、可信评分 |
| `DSecAgentEnvironment` | 把任务生命周期接到持久 rollout worker |
| worker / sandboxd | 调度、沙箱、动作日志、暂停恢复、资源回收 |

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

任务 instruction 来自任务插件，不由通用框架硬编码。TB2.1 插件调用任务原始
`tests/test.sh`，核对 CTRF 结果与完整性，再解释成二元奖励。缺失、无效或无法验证
的结果应报错并拒收样本，不能记作模型零分。

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
