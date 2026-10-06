# 最新安装候选的短 GRPO 与分叉验收

2026-10-06，source-release-r5 的端到端回归通过。该运行使用冻结核心
wheel `11b2df99f8e305c332da1dd8d2b7adce6a3a25a1d2866adea6875c296e7eef5b`，
55 个安装文件全部匹配。独立 daemon/worker 和训练容器客户端从这一
安装目录加载；没有使用旧源码作为运行时，也没有修改生产服务或权限。

## 训练与轨迹

Qwen3.5-4B，Thinking，回复上限 32768、上下文上限 65536；沿用此前
精确编码配置 `temperature=0.6, top_p=0.95, top_k=20, min_p=0.0,
presence_penalty=0.0, repetition_penalty=1.0`。Miles/Megatron LoRA 与
SGLang 在单张 16 GiB GPU 交替使用，训练侧内存补丁仍为独立实验依赖，
不进入 DSec 核心包。

两组 GRPO，每组两个独立 rollout，并发 2，任务为 TB2.1
`openssl-selfsigned-cert`，官方任务版本为
`7131e4375048a0e408a8fb404b5f499d726b695b`。每题最多 16 次工具调用，
单命令上限 120 秒、episode 上限 1200 秒；这些是本次实验配置。

| 分组 | 官方奖励 | 梯度范数 |
| --- | --- | ---: |
| 第一组 | [1, 0] | 0.265217 |
| 第二组 | [0, 0] | 0 |

训练退出码 0，耗时 **466.712 秒**。两个 checkpoint 之间 96 个 adapter
张量变化，全部有限；第一组具有学习信号，第二组为退化奖励组，不据此
宣称学习收益提高。四条 response 为 3307、5420、3078、4171 token，
logprob/mask 长度与轨迹一致，未截断、未丢弃样本，无 TITO 不匹配。

每条 episode 在首次工具执行后实际暂停、原 VMM 退出，后续工具恢复至
新 PID；sandbox 身份、generation 与步骤连续，四条可写状态独立。
训练后恢复推理并完成第二组，GPU 采样峰值 **14268 MiB**。训练容器
内存峰值 **24.716 GiB**，不作为完整 DSec 后端的资源成本。

三个官方 0 分均定位为 `test_python_verification_script` 断言失败，
不是 verifier 缺失。两条模型轨迹因 `invalid_format` 结束（其中一条
此前已完成任务且官方得分 1），另外两条达到 max_turns。保留这些终止
原因，不把得到正分等同于模型完全遵循回复协议。

## 跨 episode 准备态复用

同一安装包的独立验收入口 `tools/verify_installed_fork.py` 通过：封存
带内存状态的 HTTP 服务基线，两个新 episode 从同一计数开始，拥有新
history 和独立私有写入；第一分支官方评分 1。封存基线在双服务重启
后仍可分叉，运行分支重启保留 VMM 与已提交动作结果。第二分支存活时
停止源基线，随后暂停恢复，最后持有者释放后共享对象回收。

这证明实际准备态隔离与生命周期，不宣称分叉达到 60 ms，也不是大规模
并发性能评测。分叉 probe 的 VMM 资源计量存在暂停期间无运行进程的
采样记录，不用于资源成本对照。

## 回收与证据

最终 50 条沙箱记录全部 STOPPED，lease/pending 为 0，无 netns、私有
服务 socket、设备或活动进程引用。临时 unit 文件已恢复，原两个正式
user 服务 active；Docker、原容器、网桥和 Grafana 无变化。GPU 回到
35 MiB / 0%，磁盘剩余约 79.8 GiB。

原始模型回复、TITO、官方 CTRF/verifier/reward、worker 记录、恢复事件、
两个 adapter checkpoint、训练日志和最终审计单独保存。root 私有原始
记录通过只读挂载导出，不修改源日志权限。证据位于
`.runtime/release-v01-final/grpo-evidence/`；它们不进入公共源码或 wheel。

本次关闭最新候选的短 RL 与复用验收缺口，不代表 89 个任务全通过、
长期训练收益、verl/Uni-Agent 真实训练或第二台主机从零部署。
