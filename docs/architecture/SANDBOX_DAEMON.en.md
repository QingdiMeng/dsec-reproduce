<a id="独立守护进程与重启恢复"></a>

# Standalone daemon and restart recovery

**English** · [简体中文](SANDBOX_DAEMON.md)

Update: persistent systemd services and bounded concurrency were deployed later. See [the service deployment guide](../guides/SERVICE_DEPLOYMENT.en.md). This record covers the earlier serial service. The current deployment uses a stable service-code path. The original test paths are retained as evidence.

On 2026-09-29, work on `sandboxd.py`, `sandbox_client.py` and `durable_manager.py` was completed. All 19 service checks using real MicroVMs and all 20 existing SDK regression checks passed.

<a id="部署与调用"></a>

## Deployment and client access

The code was copied to `/home/xiaoxiaohu/dsec-reproduce/firecracker` on the server. The test service was stopped after this run. No startup unit or process supervisor was installed at this stage.

On the server, start the standalone foreground process with the following command. It listens on a local Unix socket only.

```bash
cd /home/xiaoxiaohu/dsec-reproduce/firecracker
python3 -m sandboxd \
  --root /home/xiaoxiaohu/dsec-reproduce/firecracker/service \
  --binary /home/xiaoxiaohu/dsec-reproduce/firecracker/release-v1.17.0-x86_64/firecracker-v1.17.0-x86_64 \
  --kernel /home/xiaoxiaohu/dsec-reproduce/firecracker/vmlinux-6.1.186 \
  --template /home/xiaoxiaohu/dsec-reproduce/firecracker/d-0929-204337/build/base.ext4
```

The template came from a clean guest build for this run and was kept separate from the test sandbox's writable disk. For deployment, move the template to a separate, versioned artifact directory.

From a second terminal, run the client commands below.

```python
from sandbox_client import SandboxClient

client = SandboxClient('/home/xiaoxiaohu/dsec-reproduce/firecracker/service/service.sock')
sb = client.call('create', idle_ttl_seconds=300)
print(client.call('execute', sb['id'], command='echo hello'))
client.call('pause', sb['id'])
print(client.call('execute', sb['id'], command='uname -r'))
client.call('stop', sb['id'])
```

Other operations include health, list, status, resume and recover. For recover, pass `allow_rollback=True` explicitly. The client issues an ID per request without retrying automatically. At this stage request IDs did not support durable result lookup or replay deduplication.

<a id="重启语义"></a>

## Restart semantics

| State at restart | Recovery action |
|---|---|
| Idle RUNNING sandbox with a live VMM of the recorded identity | Adopt the original process and API/vsock connections, retaining memory and current disk state. |
| PAUSED with a registered snapshot | Load the record, then validate and restore the snapshot on the next execution. |
| Operation still in flight at restart | Terminate the VMM after verifying its identity and mark FAILED/outcome_unknown. Do not replay. |
| Registered VMM no longer exists | Mark the sandbox failed. Allow an explicit rollback if it has a snapshot. |
| TTL expired during downtime | Stop the sandbox on restart and reclaim its disks and snapshots. |
| Unregistered VMM matching the service directory | Verify the user, executable and API path before terminating it. Retain its files for investigation. |
| PID belongs to another process or identity differs | Neither adopt nor signal the process using that record. |
| Corrupt registry JSON | Report registry_error and retain the record for inspection. Continue loading valid records. |

Each sandbox has a registry.json record, replaced atomically and synced to disk, and an event log. They store state, TTL deadline, snapshot generation, operation markers and process identity. Within a host boot the daemon uses the persisted monotonic deadline. Across host reboots it calculates the time remaining from the wall-clock deadline.

Process identity includes the host boot ID, PID, process start tick, UID, executable and full API socket argument. After adoption, the daemon identifies and signals the process through a pidfd. An exclusive file lock prevents two managers from owning the same service directory. The socket mode is 0600. The directory is managed under the current user's identity.

On graceful SIGTERM or SIGINT, the service preserves sandbox records and VMMs for takeover after restart. **To release a sandbox, stop the sandbox itself.** The daemon cannot enforce TTLs during downtime. It reclaims expired instances on restart.

<a id="已验证"></a>

## Verified behavior

The 19 checks covered socket permissions, adoption of the same VMM after a forced restart, memory and disk continuity, the single-instance lock, recovery of a paused record, execution after a pause, no replay after a daemon dies during execution, cleanup of its VMM, explicit rollback, TTL expiry during downtime, cleanup of expired disks and unregistered VMMs, protection of unrelated PIDs, corrupt records, preservation of VMMs on graceful restart, durable STOPPED state, error-free monitoring and absence of leftover test VMMs.

Local evidence: `results/sandboxd/result.json` and `results/sandbox-sdk-regression/result.json`. Corresponding server run directories: `d-0929-204337` and `sdk-20260929-204344`.

<a id="边界与下一阶段"></a>

## Limits and next steps at the time of testing

- The service ran serially for one trusted user on one host. A long command blocked other client requests. The background TTL thread continued to service other idle sandboxes.
- The tested version lacked automatic supervisor restart, boot integration, remote authentication, production isolation and per-tenant resource limits.
- An in-flight marker is a conservative failure indicator. An operation can complete and still have an unknown outcome if its reply was lost. Explicit rollback cannot undo external side effects.
- Tests covered SIGKILL and SIGTERM of the manager. Host power loss, disk failure and every possible gap between registry writes were outside the test. A corrupt record can leave disk files for manual inspection. The daemon avoids broad directory deletion.
- Persistence relies on trusted local files. A user who can maliciously modify their own registry lies outside this trust model.

The proposed next steps were process supervision and a stable artifact directory, followed by bounded concurrent request handling and load tests. Paper-mechanism ablations had not yet begun.
