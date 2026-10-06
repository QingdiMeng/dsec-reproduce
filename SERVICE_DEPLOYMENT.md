# 常驻服务与小规模并发验证

2026-09-29，已在 `xiaoxiaohu@192.168.0.110` 部署并启用用户级 `dsec-sandboxd.service`。用户原本已启用 Linger；没有修改其他用户服务。

## 路径与日常操作

| 内容 | 服务器路径 |
|---|---|
| 稳定服务代码 | `/home/xiaoxiaohu/dsec-reproduce/service-code` |
| 只读 guest 模板 | `/home/xiaoxiaohu/dsec-reproduce/artifacts/guest-v1.ext4` |
| 制品/代码校验记录 | `/home/xiaoxiaohu/dsec-reproduce/artifacts/deployment.json` |
| 运行登记与沙箱 | `/home/xiaoxiaohu/dsec-reproduce/live` |
| API socket | `/home/xiaoxiaohu/dsec-reproduce/live/service.sock` |
| systemd unit | `/home/xiaoxiaohu/.config/systemd/user/dsec-sandboxd.service` |

在服务器执行：

```bash
systemctl --user status dsec-sandboxd.service
systemctl --user restart dsec-sandboxd.service
journalctl --user -u dsec-sandboxd.service -n 50 --no-pager
```

使用客户端：

```python
# 在服务器 service-code 目录运行
from sandbox_client import SandboxClient
client = SandboxClient('/home/xiaoxiaohu/dsec-reproduce/live/service.sock')
sb = client.call('create', idle_ttl_seconds=300)
print(client.call('execute', sb['id'], command='echo hello'))
client.call('stop', sb['id'])
```

服务保持本地 Unix socket、0600 权限，不开放 TCP 端口。当前限制为 4 个未停止沙箱、8 个在处理的客户端连接，accept backlog 为 16。每个 microVM 仍为 1 vCPU / 256 MiB。

2026-10-01 的 [C3 对照](E5_C3_REPORT.md)后，`sandboxd.py` 增加启动参数 `--snapshot-cache-policy retain|evict`。当前常驻用户服务仍使用默认 `retain`，以保持此前延迟和缓存行为；需要在本地磁盘快照停止后回收计算 cgroup 内存的隔离部署可显式选 `evict`。该策略在快照持久化后给出 `posix_fadvise(DONTNEED)` 建议，`status` 返回请求策略与建议是否成功。真实 Firecracker/worker 对照表明内存降低但恢复延迟增加，不能把它当作无代价默认优化。常驻服务已在无活动沙箱时重启加载新代码，69 条旧记录均为 STOPPED，仍为默认 `retain`。

## 并发与过载语义

- 不同沙箱可并行执行；同一沙箱同时有操作时，新操作返回 ServiceBusy，不入执行队列。该请求 ID 在持久账本中记录已完成的错误响应；同 ID 重试仍得到 ServiceBusy，若要在竞争结束后重新发起操作，应使用新请求 ID。
- 全局请求槽已满时立即拒绝，响应可没有 request_id，客户端识别为 ServiceBusy，表示请求尚未接受。
- list 遇到繁忙沙箱返回 `id` 与 `busy=true`，避免被长命令阻塞。
- 无自动重试。ServiceBusy 表示操作未执行，调用者可在竞争结束后用新请求 ID 重新发起；RequestOutcomeUnknown 不可盲目重放。
- 创建过程仍由 manager 串行准入；本轮没有优化突发创建吞吐。

## systemd 监督与组权限

用户级 systemd 在加入 kvm 组前已启动，继承的组列表不含 kvm。因此本服务通过 `sg kvm` 启动 daemon 与启停辅助程序，使用已经授予用户的组权限，不改变设备权限。

systemd 跟踪等待 daemon 退出的 sg 主进程；使用 `Restart=always`，因为 sg 在子进程异常结束时不一定返回非零。手动 stop 仍会停止服务，不触发自动拉起。ExecStop/ExecStartPre 通过 `service_admin.py` 读取原子登记的 daemon 身份，借助 pidfd 只终止本部署的 daemon。辅助程序与 daemon 使用相同主组，以便通过 `/proc` 身份检查。

`KillMode=process` 特意保留 VMM，使其可跨 manager 重启。**停止服务不等于停止沙箱**：完整停用前先通过客户端 stop 所有沙箱，再执行 `systemctl --user disable --now dsec-sandboxd.service`。服务停机期间 TTL 不能即时执行，启动后补做回收。该行为是本原型的明确取舍，不是系统默认的子进程清理语义。

故障重启间隔为 2 秒，60 秒内最多尝试启动 5 次；超限后查看日志、修复问题，再使用 `systemctl --user reset-failed dsec-sandboxd.service` 和 start。自启已配置，未重启整台服务器验证开机过程。

## 本轮验证

15 项常驻服务检查通过，覆盖并发执行、4 VM 准入、同沙箱忙碌拒绝、8 请求槽超载拒绝、恢复健康、daemon 故障、sg 故障、手工 restart、原 VMM 与内存/磁盘连续性及监控无错误。

最终代码另通过 19 项 daemon 回归与 20 项 SDK 回归，共 54 项检查全部通过。回归结果保存于 `results/service-regression-daemon/result.json` 和 `results/service-regression-sdk/result.json`。收尾检查确认服务 enabled/active、无监控错误，登记的沙箱全部为 STOPPED。

4 个沙箱各执行 20 条 printf 命令，共 80 条，无意外失败。一次运行结果：

| 指标 | 结果 |
|---|---:|
| 80 条命令用时 | 0.659 秒 |
| 吞吐 | 121.4 条/秒 |
| 中位请求时间 | 31.9 毫秒 |
| p95 请求时间 | 39.2 毫秒 |

这些是本机小规模、已启动沙箱的短命令测量，包含登记写入/fsync；不是论文复现结果、正式性能基准或最大容量结论。

结果源：服务器 `/home/xiaoxiaohu/dsec-reproduce/load-20260929-205657/result.json`，本地 `results/service-load/result.json`。测试沙箱在验证后全部停止，常驻管理服务保留运行。

故障注入过程中修复了只读模板权限继承，以及 sg 包装进程监督、启停程序主组不一致的问题；最终配置经 daemon 崩溃、sg 崩溃和手工重启三条路径验证。

下一阶段建议开始环境分层 E1 实验；当前仍是可信单用户原型，未提供生产多租户隔离、完整磁盘配额或正式性能消融。
