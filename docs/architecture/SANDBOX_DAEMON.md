# 独立守护进程与重启恢复

后续更新：常驻 systemd 服务和有界并发已部署，见 `SERVICE_DEPLOYMENT.md`。下文记录此前串行服务阶段；当前使用 service-code 稳定路径，原测试路径仍保留作证据。

2026-09-29 完成：`sandboxd.py`、`sandbox_client.py`、`durable_manager.py`。服务级 19 项真实 microVM 测试通过，既有 SDK 20 项回归测试通过。

## 部署与调用

代码已同步到服务器 `/home/xiaoxiaohu/dsec-reproduce/firecracker`。本轮测试服务已停止；未安装开机自启或进程监督服务。

可在服务器前台启动以下独立进程；运行中只监听本地 Unix socket，不开放 TCP 端口：

```bash
cd /home/xiaoxiaohu/dsec-reproduce/firecracker
python3 sandboxd.py \
  --root /home/xiaoxiaohu/dsec-reproduce/firecracker/service \
  --binary /home/xiaoxiaohu/dsec-reproduce/firecracker/release-v1.17.0-x86_64/firecracker-v1.17.0-x86_64 \
  --kernel /home/xiaoxiaohu/dsec-reproduce/firecracker/vmlinux-6.1.186 \
  --template /home/xiaoxiaohu/dsec-reproduce/firecracker/d-0929-204337/build/base.ext4
```

模板来自本轮测试的干净 guest 构建，与测试沙箱的可写磁盘分离。后续正式部署应将此模板移动到独立版本化的制品目录。

另一个终端中调用：

```python
from sandbox_client import SandboxClient

client = SandboxClient('/home/xiaoxiaohu/dsec-reproduce/firecracker/service/service.sock')
sb = client.call('create', idle_ttl_seconds=300)
print(client.call('execute', sb['id'], command='echo hello'))
client.call('pause', sb['id'])
print(client.call('execute', sb['id'], command='uname -r'))
client.call('stop', sb['id'])
```

操作还包括 health、list、status、resume、recover。recover 必须明确传入 `allow_rollback=True`。客户端每次请求生成 ID，但不自动重试；请求 ID 目前不提供持久化结果查询或去重重放。

## 重启语义

| 重启时状态 | 处理 |
|---|---|
| 空闲 RUNNING，VMM 仍活着且身份一致 | 接管原进程和 API/vsock；内存及当前磁盘状态保持 |
| PAUSED 且有已登记快照 | 加载登记信息；执行时校验快照并恢复 |
| 重启前操作仍为 in-flight | 终止已验证身份的 VMM，标记 FAILED/outcome_unknown，不重放 |
| 记录中的 VMM 已不存在 | 标记失败；有快照时允许显式回滚 |
| TTL 在服务停止期间到期 | 重启立即停止并回收对应磁盘和快照 |
| 匹配本服务目录的未登记 VMM | 核对用户、可执行文件和 API 路径后终止，保留文件供调查 |
| PID 指向其他进程或身份不符 | 不接管、不据该记录发送信号 |
| 登记 JSON 损坏 | 报告 registry_error，保留记录供检查；其他有效记录继续加载 |

每个沙箱有原子替换并 fsync 的 registry.json 和事件日志，保存状态、TTL 截止时间、快照代数、操作标记与进程身份。同一宿主启动周期使用持久化 monotonic deadline，跨宿主重启则根据 wall deadline 计算剩余时间。

进程身份包含宿主 boot ID、PID、进程启动 tick、UID、可执行文件与完整 API socket 参数；接管后通过 pidfd 定位与发信号。服务目录使用独占文件锁，防止两个 manager 同时管理同一批沙箱；socket 权限为 0600，目录以当前用户身份管理。

SIGTERM/SIGINT 正常退出服务时保留沙箱登记与 VMM，以便重启接管。**停止管理服务不等于停止沙箱**；要释放沙箱，应先逐个 stop。服务停止期间 TTL 无法即时执行，恢复服务后才补做回收。

## 已验证

19 项检查覆盖：socket 权限、强制重启接管同一 VMM、内存连续、磁盘连续、单实例锁、暂停登记恢复、暂停后执行、执行中服务死亡不重放、关联 VMM 清理、显式回滚、停机 TTL、过期磁盘清理、未登记 VMM 清理、外部 PID 不误杀、损坏登记处理、正常重启保留 VMM、STOPPED 持久化、监控无错误、无测试 VMM 遗留。

本地证据：`results/sandboxd/result.json`、`results/sandbox-sdk-regression/result.json`。服务器对应运行目录：`d-0929-204337` 与 `sdk-20260929-204344`。

## 边界与下一阶段

- 当前是单用户、同机、串行服务；服务级请求一次处理一个，长命令会阻塞其他客户端请求，但后台 TTL 线程仍处理其他空闲沙箱。
- 没有 supervisor 自动拉起、系统启动集成、远程认证、生产隔离或多租户资源限制。
- in-flight 标记是保守故障检测；已执行但响应丢失的操作仍可能返回未知。显式回滚不能撤销外部副作用。
- 已测管理进程 SIGKILL/SIGTERM，未测试宿主断电、磁盘故障或所有注册写入间隙。登记损坏可能留下待人工处理的磁盘文件；不进行广泛目录删除。
- 当前持久化依赖受信任的本地文件；不是针对同用户恶意改写登记文件的防御。

下一阶段可先部署进程监督与稳定制品目录，再加入限并发请求处理与压力测试；论文机制消融尚未开始。
