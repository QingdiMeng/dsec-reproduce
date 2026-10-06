# 通用 microVM 环境制品发布

## 可选 fs-verity 完整性路径（2026-10-06，实机验收通过）

`artifact_integrity.py` 是不依赖任务或训练框架的完整性工具。管理员在支持
verity 的文件系统中准备只读文件，调用 `dsec-artifact-integrity seal --file
<artifact> --sha256 <已有摘要> --receipt <受保护的凭据路径>`。发布阶段先由
内核冻结文件并构建校验树，再完整读取确认原 SHA-256，最后原子发布包含
原 SHA-256、内核 fs-verity 摘要和大小的凭据；原摘要不匹配不发布凭据。
这项准备成本独立于 episode，必须单独报告。

运行时 `verify` 不读取整个数据文件；核对 root 管理的凭据与文件大小，并
通过 `FS_IOC_MEASURE_VERITY` 验证内核固定摘要，后续数据读取由内核逐页
校验。凭据和数据文件的所有祖先目录均须 root 拥有且其他用户不可写，
避免同用户伪造摘要绑定或在 VMM 打开前换掉路径。不是凭文件名、stat 或
缓存标记跳过完整性检查。选择此模式但没有内核保护时直接拒绝，不降级。

第一处接入是 verifier 工具盘，清单增加：

```json
"integrity": {
  "local": {"mode": "fs-verity", "receipt": "/var/lib/dsec/artifacts/tool.receipt.json"}
}
```

未配置这一项的现有制品仍采用全文哈希；原有 EROFS 发布器不会自动更改
制品格式或权限。复制文件会丢失 fs-verity 属性，新副本必须重新封存。
实验机根文件系统未开启 verity，因此准备独立测试卷，不修改根文件系统
特性。r5 新安装候选的 49 项核心回归通过；实机已验证写入拒绝、未封存
副本拒绝，以及离线损坏数据后读取返回 EIO。22 GiB 工具盘的冷解析约
0.516 ms，进程物理读取 0 B，解析后数据驻留页为 0；同任务配对 15 次
评分均为 1。复制与封存的一次性准备约 43.56 s，独立卷实际占用约
16.26 GiB，不能把这项成本记成零。结果与边界见
[r5 验收报告](DSEC_VERITY_PILOT_REPORT.md)。当前接入只覆盖本地 verifier
工具盘；未验证 3FS FUSE 或 DAX，也不自动替换普通 EROFS 的解析路径。
[内核接口和约束](https://docs.kernel.org/filesystems/fsverity.html)。

## 既有发布路径与历史验收

2026-10-03。通用环境现在可通过 [`artifact_publisher.py`](artifact_publisher.py) 发布，不再逐环境手工拼接文件路径和 SHA-256。输入是现有格式的 microVM 环境目录；发布器将 boot ext4、guest kernel、EROFS 层、可选 DAX VMM 与 OverlayBD lower 复制到本地内容寻址对象池。OverlayBD root-image JSON 会改写为新 lower 路径并重新固定摘要。可选 `--threefs-mount` 与 `--threefs-store` 在发布时把非 DAX 层复制到 3FS 内容寻址目录；创建时仍由环境目录选择 `local` 或 `threefs_lazy`。

发布器对源内容做完整 SHA-256 校验，拷贝期间检查源文件身份，fsync 新对象，并以不覆盖现有对象的硬链接提交。已存在对象需再次核对摘要，发现污染即拒绝发布；第二个环境引用相同字节时不会再生成一份对象。最后持锁重新读取目标目录，拒绝覆盖已有环境 ID，验证新条目的本地/远端解析，再原子替换目录文件。失败可能留下**未引用**的内容寻址对象，但不会发布半成品目录。部署目录后，daemon 需要在无活跃任务时按[运行时版本收敛](ELASTIC_RUNTIME_CONVERGENCE.md)的整套发布流程重启，才能加载新环境；工具本身不重启服务。

示例：

```bash
python3 artifact_publisher.py \
  --source-catalog candidate-catalog.json \
  --environment-id general-tools \
  --destination-catalog published-catalog.json \
  --local-store artifacts
```

需要 3FS 双来源时，再给同一命令增加 `--threefs-mount /path/to/3fs/mnt --threefs-store /path/to/3fs/mnt/objects`。源目录原有的远端引用若不重新发布，发布器仍会在发布阶段完整校验远端内容；失联挂载会拒绝发布。DAX 层始终保留本地来源。

**可信边界**：内容寻址命名和只读权限不能证明同用户无权篡改文件。运行时仍在每个 daemon 生命周期首次解析时全文哈希本地制品，后续按文件身份复核；3FS 远端只在发布阶段全文校验，创建时检查活挂载、大小和固定内容寻址名称。这次没有声称解决 daemon 冷启动的本地全文哈希，也没有启用未经验证的 `fs-verity` 或跳过校验。

在实验机的 `/dev/shm/dsec-publish-pilot` 中，发布 `general-erofs-base` 的本地制品共 **117,347,336 B**，新目录通过解析。随后使用该目录启动隔离 Firecracker VM，guest 挂载发布后的 EROFS 层、执行命令并停止，结果通过。隔离制品随后清理。验证脚本分别是 [`smoke_artifact_publisher.py`](experiments/3fs-single/smoke_artifact_publisher.py) 和 [`verify_published_microvm.py`](experiments/3fs-single/verify_published_microvm.py)；Linux 暂存源码的 15 项发布与目录测试全部通过。DAX VMM 发布后保留可执行权限有专门测试，尚未做该发布路径的实机 DAX 创建。

本次实机验证还发现 3FS 的现有 FUSE 挂载在 `stat` 中进入不可中断等待。旧 `subprocess.run(timeout=5)` 会在尝试回收该子进程时卡住，故 `environment_catalog.py` 已将全部远端路径操作移入独立探针进程；父进程超时后发终止信号且不阻塞等待。失联 FUSE 的内核进程可能直到挂载恢复才退出，故不能把此修复解释为修复了 3FS 本身。根分区后来恢复至约 94 GiB 可用，[磁盘清理记录](DISK_CLEANUP_20261003.md)说明其中只有约 6.2 GiB 能归因于本轮明确删除的旧 boot-bundle。3FS 已有对象再次读取并校验 SHA-256 通过。

在磁盘与 3FS 恢复后，按[完整运行时部署脚本](experiments/3fs-single/deploy_artifact_publication_runtime.sh)发布 84 个顶层 Python 文件。发布前仅 `environment_catalog.py` 与测试变化，新增 `artifact_publisher.py` 与测试；其余生产源码摘要一致。独立暂存版本的真实 `threefs_lazy` microVM 已通过挂载、执行、暂停恢复和 daemon/worker 双重启。生产空闲发布时 102 个记录均为 `STOPPED`，备份位于 `/home/xiaoxiaohu/dsec-reproduce/service-code-pre-artifact-20261003-235702`；发布后 102 个记录完整恢复，源码摘要一致。生产 worker 上本地及 3FS 两种来源的 DAX＋OverlayBD 沙箱均通过读层和暂停恢复，最终 104 个记录全为 `STOPPED`，队列为空，两项服务 active；生产源码 15 项相关测试全通过。发布器已安装为生产源码工具，但**未向生产目录添加仅供演示的重复环境**，下一次真实环境发布时即可使用它。
