<a id="常驻服务与小规模并发验证"></a>

# Persistent services and small-scale concurrency tests

**English** · [简体中文](SERVICE_DEPLOYMENT.md)

> Historical record: references marked “historical source not included”
> refer to experiment files outside this repository. They are not runnable
> instructions for the current release. Use the [documentation index](../README.md) for supported entry points.

See the [host configuration guide](DSEC_HOST_CONFIGURATION.md) for current deployment and Edge admission settings. This page records tests of an earlier service version.

On 2026-09-29, the user-level `dsec-sandboxd.service` was deployed and enabled on `xiaoxiaohu@192.168.0.110`. The user already had lingering enabled. No other user service was modified.

<a id="路径与日常操作"></a>

## Paths and routine operations

| Item | Server path |
|---|---|
| Stable service code | `/home/xiaoxiaohu/dsec-reproduce/service-code` |
| Read-only guest template | `/home/xiaoxiaohu/dsec-reproduce/artifacts/guest-v1.ext4` |
| Artifact and code checksums | `/home/xiaoxiaohu/dsec-reproduce/artifacts/deployment.json` |
| Runtime registry and sandboxes | `/home/xiaoxiaohu/dsec-reproduce/live` |
| API socket | `/home/xiaoxiaohu/dsec-reproduce/live/service.sock` |
| systemd unit | `/home/xiaoxiaohu/.config/systemd/user/dsec-sandboxd.service` |

On the server, run the following commands.

```bash
systemctl --user status dsec-sandboxd.service
systemctl --user restart dsec-sandboxd.service
journalctl --user -u dsec-sandboxd.service -n 50 --no-pager
```

To call the service from a client, run the following commands.

```python
# 在服务器 service-code 目录运行
from sandbox_client import SandboxClient
client = SandboxClient('/home/xiaoxiaohu/dsec-reproduce/live/service.sock')
sb = client.call('create', idle_ttl_seconds=300)
print(client.call('execute', sb['id'], command='echo hello'))
client.call('stop', sb['id'])
```

The service listens on a local Unix socket with mode 0600. It exposes no TCP port. The configuration allows four non-stopped sandboxes and eight active client connections, with an accept backlog of 16. Each MicroVM has one vCPU and 256 MiB of memory.

Following the C3 comparison on 2026-10-01 (`E5_C3_REPORT.md`, historical source not included), `sandboxd.py` gained the startup option `--snapshot-cache-policy retain|evict`. The persistent user service retained the default `retain` to preserve the earlier latency and caching behavior. Isolated deployments that need to reclaim compute-cgroup memory after stopping a local-disk snapshot can explicitly select `evict`. That policy issues `posix_fadvise(DONTNEED)` after the snapshot is durable. `status` reports both the requested policy and whether the advice succeeded. The real Firecracker/worker comparison measured lower memory usage and higher resume latency. The service was restarted with no active sandboxes. All 69 historical records were STOPPED, and the policy remained `retain`.

<a id="并发与过载语义"></a>

## Concurrency and overload

- Different sandboxes can execute in parallel. If one sandbox already has an operation in progress, another operation on it returns ServiceBusy without entering the execution queue. The durable ledger records that error as the completed response for that request ID. Retrying the same ID still returns ServiceBusy. Submit a new ID after contention clears to start a new operation.
- When all global request slots are occupied, the service rejects the request immediately. The response may omit request_id. The client maps it to ServiceBusy, indicating that the request has not been accepted.
- For a busy sandbox, list returns `id` and `busy=true` so that a long command does not block inventory queries.
- The client does not automatically retry. ServiceBusy means the operation did not execute, so callers can submit a new request ID when contention clears. Never blindly replay RequestOutcomeUnknown.
- The manager still admits sandbox creation requests serially. This test did not optimize burst creation throughput.

<a id="systemd-监督与组权限"></a>

## systemd supervision and group permissions

The user's systemd manager started before the user joined the kvm group. Its inherited group list therefore lacked kvm. The service starts the daemon and the start/stop helpers through `sg kvm` to use the group rights already granted to the user. Device permissions are unchanged.

systemd tracks the sg process that waits for the daemon. The unit uses `Restart=always` because sg may return zero after an abnormal child exit. A manual stop keeps the service down. ExecStop and ExecStartPre use `service_admin.py` to read the atomically recorded daemon identity, then use a pidfd to terminate only this deployment's daemon. Helper and daemon run under the same primary group so the `/proc` identity check succeeds.

`KillMode=process` deliberately preserves VMMs across manager restarts. **Stopping the service leaves its sandboxes running.** To shut down the entire deployment, first stop every sandbox through the client and then run `systemctl --user disable --now dsec-sandboxd.service`. TTLs cannot be enforced during downtime. The service reclaims expired instances when it comes back. This behavior is an explicit choice in the prototype.

After failure, systemd retries after two seconds, with a limit of five starts in 60 seconds. If the limit is hit, inspect the logs, fix the cause, then use `systemctl --user reset-failed dsec-sandboxd.service` and start. Automatic startup was configured but not tested by rebooting the server.

<a id="本轮验证"></a>

## Test results

All 15 persistent-service checks passed. They covered concurrent execution, admission of four VMs, rejection of contending operations on one sandbox, overload rejection at eight request slots, return to healthy service, daemon and sg failure, manual restart, continuity of the original VMM and its memory and disk, and error-free monitoring.

The final code also passed 19 daemon and 20 SDK regressions, for 54 successful checks in total. Results are in `results/service-regression-daemon/result.json` and `results/service-regression-sdk/result.json`. At teardown the service was enabled and active, monitoring reported no errors, and every registered sandbox was STOPPED.

Four sandboxes each executed 20 printf commands, for 80 commands without unexpected failures. The table gives one measured run.

| Metric | Result |
|---|---:|
| Time for 80 commands | 0.659 s |
| Throughput | 121.4 commands/s |
| Median request time | 31.9 ms |
| p95 request time | 39.2 ms |

These short commands ran on already started sandboxes on a single host and include registry writes and fsync. Their timings characterize this test only. They do not establish a paper reproduction result, a formal benchmark or the maximum capacity.

Server evidence: `/home/xiaoxiaohu/dsec-reproduce/load-20260929-205657/result.json`. Local evidence: `results/service-load/result.json`. All test sandboxes were stopped after validation. The persistent management service kept running.

Fault injection exposed read-only template permission inheritance, supervision of the sg wrapper and mismatched primary groups in the start/stop helpers. The corrections were verified by crashing the daemon, crashing sg and restarting the service manually.

The proposed next experiment was E1 environment layering. This stage remained a trusted single-user prototype. Production multi-tenant isolation, complete disk quotas and formal performance ablations were outside its scope.
