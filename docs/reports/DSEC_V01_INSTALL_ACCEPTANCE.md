# 单机 v0.1 安装与部署验收

## 2026-10-08：模块化 R3 实机回归

本节对应重构基线 `775b5be` 加本轮持久化/忙碌拒绝修复，不覆盖下面历史 r5
证据的版本归属。核心安装 wheel SHA-256：
`63839e2000c72045ed662e4b66558b16e9ad4ea546cb496f694cf53701010f0e`。
独立 venv 使用 installed wheel 和 Python isolated mode，TB2.1/MBPP 为分别
安装的可选应用。复用已有实验机及固定外部制品，不等同于第二台全新主机验收。

| 验收 | 本轮结果 | 原始证据文件 |
| --- | --- | --- |
| Linux 发行回归 | 323 项：322 通过，1 项因发布包不含实验目录别名跳过，无失败/错误 | `installed-linux-busy.json` / `.log` |
| 非 TB 生命周期 | 自动监督、同一 VMM 恢复、暂停重启、动作去重、评分 1、最终回收 | `counter-recovery-fixed.json` |
| 旧 VM 升级 | 一台 RUNNING、一台 PAUSED，原 registry、身份、私有文件与动作记录保留，新 Edge 接管两条租约 | `live-old-to-new-fixed.json` |
| 旧容器升级 | 旧安装版客户端及旧 standalone agent 创建的容器，由新 Edge 接管；容器 ID 不变、动作不重复、最终租约归零 | `container-old-to-new.json` |
| Container Edge 重启 | 真实 EROFS/OverlayFS、network=none，容器/租约 ID 不变，私有写入保留并正常清理 | `container-restart.json` |
| SDK/worker 共享准入 | SDK 占满 CPU 预留后，worker 按 cpu_budget 等待；释放后准入，最终无租约 | `shared-admission-fixed.json` |
| ready 租约交接 | 同一 VM、同一节点租约；CPU 预留 0.05→1，内存估计保持 512 MiB；客户端池命中 45.68 ms | `ready-handoff.json` |
| TB2.1 官方验证 | openssl-selfsigned-cert，经 live/paused 重启、动作去重，官方 verifier 评分 1 | `tb21-openssl-final.json` |
| 准备态分叉 | 同一基线的两个顺序 episode，内存计数分别从 8→9、私有写入隔离、源删除后恢复、最终 CAS 回收 | `tb21-prepared-fork-fixed.json` |
| MBPP 固定候选 | 64/64 符合预期：正确/隔离为 1，错误/超时/提前退出为 0；实际节点租约最高 16 | `mbpp-execution.json` / `mbpp-receipts/` |
| 原生短 GRPO | Qwen3.5-2B + verl/SGLang，n=8，四步、非零梯度、模型/优化器检查点和逐条证据链接通过 | `rl-four-acceptance.json` |
| 真实 3FS 来源 | 双副本就绪，48,201,728 字节 EROFS 对象发布并校验；两台 VMM 的实际文件句柄指向 3FS，写隔离及一次暂停恢复通过，最终无租约 | `threefs-ready.json` / `threefs-erofs-acceptance-final.json` |
| 新旧版本固定动作开销 | 同宿主、同只读制品缓存、同声明资源，每版 4 台新建 file-ext4 VM，创建/执行均完成并回收 | `paired-revision.json` |

创建墙钟中位数：旧版 782.35 ms、新版 792.02 ms；固定动作执行中位数：
旧版 47.80 ms、新版 42.80 ms。样本小、固定顺序，包含调度/RPC，不足以作
统计显著性或吞吐优势声明。ready 的 45.68 ms 只对应一次已启动 VM 的取出，
不是冷创建、分叉恢复、完整 episode 或规模下的 p95。声明 CPU/内存需求是
调度预留，不是 CPU 利用率或实测 RSS；两者不互换。MBPP 的 32 请求并发受
16 槽节点预算约束，不能称为 32 台 VM 同时执行。

短 GRPO 保持 Non-Thinking、8192 回复上限、LoRA rank/alpha 8/16、MLP targets、
temperature 0.7、top_p 0.8、top_k 20、min_p 0、presence_penalty 1.5、seed 42。
训练四步分别评分 8/16、15/16、8/16、0/16；最终小规模验证为 8/16。
第 2 步存在混合奖励，梯度范数 0.255859375；其他步骤的零/极小梯度不都代表
任务学习信号。80 条生成中 72 次沙盒执行、8 次明确标注的模型格式零分；
停止原因 74 `stop`、6 `length`，不以动态 batch clip_ratio 判定截断。所有
真实执行已清理；TITO、mask/logprob 长度与不可变生成/回执链接一致。此轮
只验收重构后的原生训练接入，不构成完整 MBPP 成功率或训练前后收益对照。
先前单步 `rl-acceptance.json` 的两组全 1/全 0，梯度为零，单独不能证明有效更新。

本轮实机暴露并修复两项核心缺陷：并发发布预算共用固定 `.tmp`，以及短暂锁
竞争被永久提交成固定 request ID 的忙碌失败。分别采用独立临时 inode 和
明确未接纳时取消 journal intent；新 capability 限定 SDK 的同 ID 有界等待，
UNKNOWN 仍禁止重放。服务就绪工具允许 120 秒，覆盖启动时的大制品完整性校验。
验证不以扩大执行超时掩盖模型或 verifier 问题。

首次失败均保留：错误 smoke guest、测试编排的准入等待、旧代理地址导致联网
失败、服务就绪上限不足、分叉时操作锁竞争，以及测试目录继承 0775 被权限
预检拒绝。后者只修正本轮创建的专用目录，不放宽宿主权限检查；升级现有部署
仍需操作员核对目录模式，见 [主机配置](../guides/DSEC_HOST_CONFIGURATION.md)。

证据保存在实验机的 `dsec-r3-20261008/evidence/`，本地镜像归档在外层工作区
`.runtime/r3-20261008/remote-evidence/evidence/`，完整短训练 TITO/回执保存在
`.runtime/r3-20261008/rl-four-evidence/`；不将机器配置、凭据、模型或生成状态
放进开源源码目录。宿主 Docker/Grafana 未重启，专用测试容器已按 SDK 回收。

R3 的计划内功能关卡已通过。3FS 回归使用停止后备份的独立副本，保持原备份
不变；管理员恢复原配置的 `dsec3fs0` / `198.18.73.1/32`、Soft-RoCE 和专用
挂载传播范围。克隆服务在 123.85 秒后达到双副本 SERVING-UPTODATE 和 FUSE
就绪。旧运行镜像缺少备份中 metadata/storage 二进制依赖的 jemalloc，已从
Ubuntu 签名 APT 提取 `libjemalloc2 5.2.1-4ubuntu1`，只放进测试依赖目录，
没有安装宿主包；四个主要程序的动态库检查均通过，版本/哈希单独归档。
该依赖存在于原备份二进制中，不是重构新增。旧容器记录没有额外库路径或库挂载，
但没有保留可确认 jemalloc 安装来源的记录，不能断言来自旧容器可写层。
外部服务部署需固定完整运行依赖，并在目标运行镜像内检查动态库闭包。

两台 VM 读取同一个 3FS 上的 base EROFS 对象，其余三个层仍为本地来源。
宿主 VMM 文件描述符直接确认该来源，没有以 storage 标签代替实际证据。
第一次采样身份与 VMM 组不同，被 proc 权限限制；换成已授予的 kvm 组后两次
采样均无错误。一次 VM 暂停恢复保持私有文件，下一 VM 不继承该文件，最终
节点租约为零。发布时的完整哈希校验已预热该对象，因此本轮不证明冷缓存
读取字节量、按需性能收益、全部层均来自 3FS 或分布式存储行为。

3FS 设备识别、旧专用 NIC、缺失动态库、挂载多行解析和制品目录写权限的首次
问题也保留在测试记录中；这些是外部服务恢复/验收编排问题，本轮没有因此修改
DSec 的底层存储机制。临时存储服务在验收后正常停止；管理员准备的专用网络
设备、私有 bind 挂载及测试副本保留，便于后续复验，不声称它们已删除。

后续 r5 安装候选通过 49 项核心回归、工具盘真实完整性检查及 15 次
固定轨迹评分；最终 43 条记录 STOPPED，无租约/pending/netns/活动设备
或进程引用，宿主无干扰。冷解析与成本结果见
[r5 报告](DSEC_VERITY_PILOT_REPORT.md)。下面先前功能与 GRPO 证据的
版本归属不变；r5 随后已完成独立短 GRPO、跨 episode 分叉隔离及重启
回归，最终 50 条记录停止、无资源残留，见
[最新训练验收](DSEC_V01_GRPO_ACCEPTANCE.md)。

日期：2026-10-06。发行入口阶段完成；完整开源交付仍按
[两天计划](../../ROADMAP.md)继续。此处是功能验收，不是吞吐或
Docker 成本优势报告。

冻结开发 wheel：`dsec_reproduce-0.1.0.dev0-py3-none-any.whl`，183697 字节，
SHA-256 `53b0891380d0c57b4bca87adcdaa69eb5325773fdc6fe907abc8f1e864f2ce09`。
打包边界检查通过：51 个 Python 模块、3 个插件数据文件及发行元数据；
第三方 Miles 许可文本随包保留。核心没有第三方 Python 运行依赖，模型与
训练框架需要各自的依赖。

## 验收与证据

| 项目 | 实机结果 | 原始记录 |
| --- | --- | --- |
| 独立 venv、离开源码目录、禁用源码路径 | 安装与插件导入通过，54 个运行文件逐字节匹配 wheel | `.runtime/release-v01-pilot/pilot-final-audit.json` |
| 非 TB 任务 | 创建、动作去重、暂停恢复、评分 1、停止与租约释放通过 | `.runtime/release-v01-pilot/acceptance-supervisor-r8.json` |
| 自动监督 | 杀死隔离实例 launcher 后自动恢复；同一 worker 与原 VMM 保留 | 同上，`automatic_supervision` / `live_restart` |
| 暂停后双服务重启 | 原私有文件、PAUSED 状态与租约恢复，通过评分与最终释放 | 同上，`paused_restart` |
| TB2.1 安装版 | openssl-selfsigned-cert，官方 tests/test.sh 评分 1 | `.runtime/release-v01-pilot/tb21-acceptance-r8.json` |
| TB2.1 双服务重启 | 同一 VMM、已提交动作不重放；暂停恢复后官方评分 1 | `.runtime/release-v01-pilot/tb21-acceptance-restart-r7.json` |
| 官方证据 | reward.txt、verifier.log、ctrf.json、manifest.json 已归档 | `.runtime/release-v01-pilot/evidence/` |
| 冻结版本短 GRPO | 两组 × 两样本，官方评分 `[0,1]` / `[0,0]`；两次训练切换、4 次真实快照恢复、TITO 和回收通过 | `.runtime/release-v01-pilot/grpo-evidence-r8/installed-grpo-audit.json` |
| 安装版准备态复用 | 同一基线生成两个新 episode；官方评分 1、重启、源删除后恢复与 CAS 回收通过 | `.runtime/release-v01-pilot/installed-fork-r8.json` |
| 最终回收审计 | 两个当前临时实例的 15 条沙箱记录全部 STOPPED，无待执行动作、租约或 netns；临时服务关闭 | `.runtime/release-v01-pilot/post-grpo-fork-audit-r8.json` |

本地 57 项相关回归通过，包括任务插件、对话、worker、封存/分叉、网络、
发行边界与新部署入口。随后在 Linux 实验机用 Python isolated mode 加载
测试、只导入已安装的 wheel，另有 31 项资源预算、准入拒绝、队列恢复、
SDK 和共享分叉边界回归全部通过，无跳过；记录为
`.runtime/release-v01-pilot/resource-tests-r8.json`。TB2.1 保持任务修订
`7131e4375048a0e408a8fb404b5f499d726b695b`。评分轨迹是固定解题命令，
不代表模型的解题成功率，也不代表全部 89 个任务通过。

TB2.1 实例使用真实 EROFS 分层、OverlayBD＋ublk 私有根盘、独立 netns
和已配置 Mac 临时代理；普通 Firecracker 由新安装的受限助手按固定哈希
启动。两槽地址段为 4096–4097，与既有实例分离。常驻 ublk 服务仍沿用
先前系统部署，临时实例存储位于其已授权根下的独立子目录。

## 本轮关闭的部署缺口

任务实现迁入可安装的 `dsec_adapters`，旧 import 仅作别名；worker 官方
verifier 直接使用安装包。主机配置集中描述制品、调度和网络，不依赖实验
源码目录。服务启动使用 Python isolated mode。

ublk 服务需要存储目录的组搜索权限及其 systemd 写目录授权。控制目录为
仅组搜索的 2710，worker 记录保持私有；不能只靠 chmod 绕过服务的挂载
命名空间。已有 TB2.1 guest 制品启动时要求虚拟网卡，因此无网配置不能
作为这些制品的有效部署。两项首次失败均保存记录并释放租约。

旧部署还通过独立环境变量固定 verifier manifest；新部署遗漏它会错误地
比较旧工具盘哈希。现已从同一个主机配置派生 daemon 与 worker 的默认/
按任务 verifier pins，新增回归检查。失败记录保留在 r4/r5/r6 文件中。

服务启停检查支持安装后的模块入口，保持相同组身份的 pidfd 检查；
ExecStop 明确停止 daemon。`Restart=always` 处理 sg 子进程返回码语义，
worker 使用弱依赖保持 daemon 自动恢复期间的任务和租约。

## 交付边界

这验证了实验机上的独立安装实例，复用了已有模型、镜像、内核、ublk 和
只读制品。全新 Linux 主机从零部署、全部可选存储条件、
完整后端 Docker 对照仍是后续门槛；源码交付审查结果见下文。训练内存补丁只在
显式 training-experiment 包中，不计作 DSec 发行功能。

## 冻结运行时短 GRPO

Qwen3.5-4B thinking、32768 回复上限、两组 × 两样本、并发 2，474.759 秒
退出码 0。运行时 54 个文件逐字节匹配上述冻结 wheel；训练侧使用显式实验
包，任务实际运行在安装后的独立 daemon/worker。

两组官方评分为 `[0,1]` / `[0,0]`。第一步梯度范数 0.256775，第二步为 0；
两个 checkpoint 之间 96 个 adapter 张量不同且均有限。第二组奖励相同，
不能据此宣称第二步具有有效学习信号或模型解题能力提高。

四个 episode 的 response 分别为 4603、4967、4449、4220 token，logprobs、
loss mask 与 TITO 一致。每条轨迹在第一次工具调用后实际 PAUSED、退出原
VMM，再由后续工具调用恢复到新 VMM，沙箱身份和步骤连续；四条独立写盘
使用相同的 6 个 EROFS 源层。此项证明组内独立 episode 与每条 episode 内
恢复，不替代跨 episode 准备态分叉验收。

三个 0 分轨迹的官方 CTRF 均定位到 `test_python_verification_script` 断言
失败，其中一条 agent 提前因 `invalid_format` 结束；不是缺失 verifier 评分。
正负分的官方日志、原始回复、TITO、worker 记录、checkpoint 和资源采样已
保存。GPU 采样峰值 14406 MiB，训练容器 memory peak 20.525 GiB，两者只
描述本次训练实验，不能作完整 DSec 后端成本。四条记录最终 STOPPED、
无 pending、无租约；先前安装回收审计仍只对应 GRPO 启动前的时点。

## 安装版跨 episode 准备态复用

`tools/verify_installed_fork.py` 通过同一个安装 venv、统一主机配置、正式 SDK
和任务插件验证。两槽实例采用一个封存基线加一个分支，两条 episode 顺序
创建；它不替代此前四分支的并发性能结果。

基线中准备的 HTTP 服务保留真实内存计数，新分支均从计数 8 和新的 policy
history 开始；第一分支的私有文件和答案没有进入第二分支。第一分支官方
评分 1。封存后双服务重启仍可分叉；运行分支重启保持原 VMM，重复已提交
动作返回相同观察。第二分支存活时停止源基线，共享 lower inode 保留；
该分支随后暂停恢复，私有文件和应用计数继续。最后三个 rollout 全部
STOPPED、无 pending、无租约，最后一个持有者释放后共享对象删除。

最终审计再次核对 54 个安装文件与冻结 wheel 匹配，两个当前临时实例共
15 条沙箱记录均停止，实例 netns 为空，四个临时服务关闭；原两个正式服务
正常且队列为空。GPU 空闲采样 35 MiB / 0%，根盘剩余约 97.6 GiB。

## MIT 候选包

用户选择项目自身代码与文档为 MIT。Miles 派生模块保留 Apache-2.0，wheel
组合许可表达式为 `MIT AND Apache-2.0`，随包包含两份许可与第三方来源说明。
新的开发候选包 184823 字节，SHA-256
`38fd9bb7602aad10b0bcbc816bf71e8d422624de41cb6d9659a8d0566f6f4a7e`。

许可和 README 元数据调整没有改变 54 个运行文件；与上述功能冻结 wheel
逐字节相同。在实验机另一个全新 venv 安装候选包，通过 isolated import、
许可元数据和全部运行文件核对。记录为
`.runtime/release-v01-mit/acceptance.json`。新版 wheel 边界检查为 51 个模块、
62 个条目，许可文件内容也逐字节检查；两种运行包的许可包含检查通过。
历史功能证据继续保留原 wheel 哈希，不将元数据变化冒充重新运行的训练。

## 从源码重建与可选应用安装验收

白名单源码候选 r2 含 96 个文件（包括清单），251944 字节，SHA-256
`eba9161c2e4abfbbac7117795df66dec9dbb0ee44a1b1f3e949f4c6c57f06528`。
每个文件在 `SOURCE_MANIFEST.json` 固定摘要；归档可重复生成相同字节。
源码不含任务数据、实验结果、镜像、模型、密钥或 GPU 训练补丁。
实验机使用固定 setuptools 80.9.0 / wheel 0.45.1 从该归档离线构建：

| 包 | 字节 | SHA-256 |
| --- | ---: | --- |
| 核心 | 185063 | `dc86af4068679aca8ea5aa2abd591ee6cb21857d15e776f751e77257ad41e54a` |
| 可选 TB2.1 应用 | 13460 | `f486c6c92e415f5033d9263eab28dd64cf90d39f5a592d3340bf87cb8dceefc4` |

新的 venv 先只安装核心；确认应用包不存在，并核对全部 54 个运行文件
与源码清单相同。Linux 上 33 项核心回归通过，无跳过。用归档中的 C agent
和本地固定镜像新建 64 MiB 非 TB guest，运行评分 1、自动监督、动作去重、
运行态及暂停态恢复、租约释放全部通过；未复用旧 guest 模板。

随后显式安装应用包，安装版 5 项应用回归通过。应用校验全部 89 个任务
文件的修订与哈希；这项不是 89 题运行或评分通过。代表任务
`openssl-selfsigned-cert` 的现有 OverlayBD 注册路径和新转换六层 EROFS
加 file-ext4 路径均完成相同轨迹及官方评分 1，原始 CTRF/reward/log 已保存。
新制品路径创建 1.463 s、verifier 25.742 s，仅为功能验收的单次计时。
同一安装版的 OverlayBD 准备态分叉验证两条 episode 的 history/私有文件/
内存隔离、源删除后恢复以及最后共享对象释放通过；此轮没有额外重启
TB 服务，前述重启证据仍对应原冻结版本。

新制品启动暴露父目录 setgid 继承问题。先保留配置不匹配的拒绝记录，
随后发现 file-ext4 沙箱目录继承为 2700，被限定 700/2770 的启动器拒绝。
创建时将私有目录明确设为 700，OverlayBD 仍按需设为 2770；没有放宽
root helper 或目录共享范围。该修复是本轮相对原冻结版本唯一运行代码
变化（`sandbox_sdk.py`），新增两项 Linux 权限回归。此前 GRPO 结果未重跑。

最终审计包含两个源码实例各 1 条、共享 TB 验收实例 19 条沙箱记录，均为
STOPPED；worker 的历史 FAILED 记录保留，但没有 pending 或 lease。
实例 netns 为空，6 个相关临时服务 inactive，两个正式服务 active 且队列为空，
根盘剩余约 97.26 GiB。原始验收、官方日志和失败部署记录保留在实验机
`v01-pilot/source-release{,-r2}` 与本地 `.runtime/release-source-acceptance/`。
最后交付归档允许纳入本次文档更新，另以交付清单固定最终摘要；它的核心
与应用代码必须与上述已测源码逐字节一致，不将文档变化视为新增运行测试。

## 宿主隔离与权限修复候选 r3

2026-10-06，新源码归档含 98 个文件，255752 字节，SHA-256
`d4070cfd4cbe726f9e957b251169761dcc23a6556d393933a217cd809df21cf3`。
实验机离线重建的核心 wheel SHA-256 为
`ee370cb1ae15b736479eac53dc8e510f08b2e2d8ffdfc5237f0618283f1e9b6a`。
新 venv 的 40 项核心回归通过，全部 54 个运行文件匹配归档；之后显式安装
原可选 TB2.1 应用包。guest 模板与既有任务制品复用，没有冒充重新构建。

两个核心改动：创建 OverlayBD 根盘前检查 root daemon 的真实 umask，拒绝
不能保留组权限的配置；网络 helper 在状态目录、锁和网络操作之前核对
宿主 mount/net 上下文。上游 Rust 创建不存在运行目录的协议没有改变。
实验启动器另将私有 Docker 的 mount/net 均隔离；DSec 服务仍在宿主管理
可持久化的 guest netns 句柄。r1/r2/r3 失败实验入口退役，证据保留。

管理员安装精确 helper 修复并移除一个已确认失效的空句柄后，正常 SDK
恢复并停止原失败 rollout。r4 使用新包实测两种错误 helper 上下文的拒绝，
真实 root ublk daemon 的 0027 umask 被非 root 客户端在 RPC/设备/运行目录
分配前拒绝。随后 Docker 6 次、DSec 新建 6 次、准备态分叉 3 次均评分 1。
宿主服务与原容器 PID、docker0 和 Grafana 健康状态保持不变。最终私有
实例 33 条记录全部 STOPPED，无租约、pending、所属 netns、服务 socket
或遗留设备引用。26 项安装版实验/协议/权限回归也全部通过，无跳过。

证据位于 `.runtime/release-pair-r4/`；完整成本报告见
`.runtime/release-pair-r4/report/REPORT.md`。这次不是短 GRPO 重跑，也不代表
全部 89 个任务评分通过或所有生产崩溃模式已经覆盖。成本结果未显示整体
优于 Docker：启动全文校验 verifier 工具盘造成大量宿主文件驻留；此问题
与 guest/VMM 匿名内存区别记录，不通过改变统计口径隐藏。
