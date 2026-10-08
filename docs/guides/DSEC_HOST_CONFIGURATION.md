# Installed single-host configuration

`dsec-host` is the wheel's configuration and acceptance entry point. The core
control plane and task plugins are installed together; images, task definitions,
kernel, Firecracker and optional storage services remain explicit artifacts.
Configuration is JSON with schema 1. Relative paths resolve from its directory.

```json
{
  "schema": 1,
  "instance": "local",
  "state_root": "/srv/dsec/local",
  "service_group": "kvm",
  "sandbox": {
    "binary": "/opt/dsec/firecracker",
    "kernel": "/opt/dsec/vmlinux",
    "template": "/opt/dsec/guest.ext4",
    "capacity": 2,
    "snapshot_concurrency": 2,
    "snapshot_strategy": "full",
    "snapshot_cache_policy": "retain"
  },
  "worker": {},
  "scheduler": {
    "cpu": 2,
    "memory_mb": 1024,
    "disk_mb": 2048,
    "network_mbps": 1000,
    "api_episode_slots": 2,
    "api_inflight": 2,
    "api_rpm": 60,
    "api_tpm": 100000,
    "min_memory_free_mb": 1024,
    "min_disk_free_mb": 4096,
    "network_interface": "eth0"
  }
}
```

These are example admission budgets, not a measured concurrency limit or guest
memory limit. Actual VM limits come from the selected environment. The runtime
user must own its state directory, with mode 0700, and have write access to its
parent. The path must fit Linux's Unix-socket length limit. Each instance derives
independent daemon sockets, worker sockets, rollout records and scheduler budget
under `state_root/sandboxes` and `state_root/worker`.

### Container runtime ownership

The sandbox service owns container backends, artifact resolution and lifecycle
journals. `DSecClient` is a transport-only SDK; its caller needs the service
socket rather than local image paths or Docker access. `health` advertises
`container-rpc-v1`. A new client refuses container calls against an older
service; it does not fall back to managing host Docker itself. Upgrade the
client and sandbox service from the same revision.

For schema-1 compatibility, `worker.container_root`, `worker.container_catalog`
and `worker.container_agent` remain configuration keys, but `dsec-host` applies
them to the **sandbox service** as `DSEC_CONTAINER_ROOT`,
`DSEC_ENVIRONMENT_CATALOG` and `DSEC_CONTAINER_AGENT`. Paths resolve from the
configuration file. `worker.docker_broker_socket` is applied to both services:
Edge uses it for runtime operations and the worker's existing resource monitor
uses it for observation. Other legacy E1/E2 artifact variables, if used, must
also be supplied to the sandbox service rather than the training process.

Set `worker.container_agent` to the standalone installed implementation:

```bash
python -c 'import dsec.runtime.backends.container_agent as agent; print(agent.__file__)'
```

The legacy `container_runtime_agent` module is an import compatibility bridge.
Binding that bridge alone into a tools image without the DSec package does not
provide the standalone agent. Existing custom agents remain explicit paths.

The container directory has one Edge owner lock. Existing
`lifecycle-requests/*.json` retain their request IDs, operation names, digests
and schema. An in-flight or uncommitted result stays UNKNOWN until a read-only
attestation proves completion; a request is never repeated to obtain that proof.
Formal host deployments use the Edge node budget for both container and microVM
creation. The legacy worker admission socket is incompatible with `--node-budget`;
`dsec-host run sandbox` clears the inherited legacy socket environment setting.
Closing a client or normally retiring the service does not stop live containers;
release owned sandboxes through the SDK before removing their instance.

R3 real-host acceptance preserved one old-client-created container through new
Edge adoption and preserved another through service restart, including action
deduplication, private writes and final lease release. See the
[installation acceptance](../reports/DSEC_V01_INSTALL_ACCEPTANCE.md).
The host
container backend retains its trusted single-host development/comparison scope;
this change does not implement container containment inside a VM, container
pause/memory offload or a general container inventory/TTL controller.

With OverlayBD enabled, `state_root` and its `sandboxes` directory use mode
2710 and group `kvm`: the storage daemon can traverse them but cannot list or
write the control directory. Worker records and control sockets remain private;
individual disk runtime directories receive the existing storage group access.
Parent directories must also permit traversal by the storage service, and that
service's mount namespace must allow writes to the chosen sandbox root (for
example its systemd `ReadWritePaths`). Changing file mode alone cannot grant
that systemd write scope. Provision this together with the storage service.

`service_group` refreshes an already granted group using `sg`. Omit it when the
service manager already has the required groups. It grants no new permissions.
User service units run the venv Python in isolated mode and include matched
start/stop identity checks. `Restart=always` handles `sg` returning zero after a
child failure; explicit systemd stop does not restart the unit. Sandbox unit
stop preserves VMMs. Shut down sandboxes via the SDK before retiring an instance.

The worker has a weak `Wants` dependency on the sandbox service, so automatic
daemon recovery does not stop the worker or release its live leases. The restart
smoke also kills this isolated instance's supervised launcher and checks automatic
recovery with the same worker and VMM, then tests paused recovery.

## Optional catalog, storage and task plugins

Add `sandbox.microvm_environment_catalog` for pinned environment recipes.
Recipes independently select local/3FS layers, DAX and file/OverlayBD roots;
3FS is optional. OverlayBD needs `sandbox.overlaybd_ublk_socket` and
`sandbox.overlaybd_global_config`. Hashes, layer layout and guest resource limits
are validated by `run sandbox --validate-only` and before serving them.

For TB2.1 add `worker.tb2_tasks_dir`, `sandbox.tb2_manifest`, and the chosen
`tb2_verifier_artifact_manifest`/`tb2_verifier_artifact_local`. The installed
`dsec_adapters.tb2_dsec_environment.TB2DSecEnvironment` accepts the task directory
and catalog directly. Trainer-side environment configuration prefers
`DSEC_TB2_TASKS_DIR`; the old `OPENENV_TB2_TASKS_DIR` is an import compatibility
option and does not require OpenEnv. Official scoring uses the task's
`tests/test.sh`; diagnostic offline modes must not be reported as official scores.

The canonical verifier inherits the guest's network and proxy configuration.
Mounting a dependency cache does not force uv offline or prove that a task's
dependencies are complete. The optional worker-service environment override
`DSEC_TB2_VERIFIER_ONLINE=0` explicitly selects uv offline mode; `=1` permits uv
network access within the existing guest network policy. Neither grants network
access or changes firewall rules. With no override, uv uses its environment
defaults. Cache/tool compatibility and dependency preparation are case specific.
Failed verification exports available logs, reward/CTRF files and a failure
receipt before VM cleanup. Missing files and export failures are recorded;
a bootstrap failure without a valid CTRF report remains an unscored episode.

An isolated network configuration has `helper`, `max_slots`, `dns`, and optional
`dax_binary` fields under `network`. It requires a catalog. The helper must be
root-owned, non-writable by the runtime user and scoped to that instance's
`state_root/sandboxes`. Its pinned ordinary/DAX binaries must match the catalog;
the explicit DAX path selects the helper's approved alias. Do not reuse a helper
scoped to a different runtime root. Helper and storage provisioning are separate
administrator steps; `doctor` changes no permissions.

An instance can keep its HTTP proxy while allowing selected software sources
to connect directly. Set `sandbox.egress_proxy_url` and, for example,
`sandbox.egress_proxy_bypass_host` to
`["archive.ubuntu.com", "security.ubuntu.com"]`. The daemon adds these hosts
to both `no_proxy` and `NO_PROXY` for guest commands, including ordinary
`apt-get` commands. Other destinations continue to use the proxy. This is an
explicit operator setting with no built-in Ubuntu exception; it changes proxy
selection, not the network helper's firewall rules. Verify direct connectivity
from the guest before enabling it. Hostnames are validated and bounded to 32.

`worker.metrics_port` optionally binds the existing Prometheus endpoint to
localhost. `scheduler.shared_services` uses the existing shared-service monitor
configuration. API budgets apply to worker `policy_call` accounting; model calls
made directly by an external trainer need corresponding client-side rate control.

The existing resource and API fields in `scheduler` retain their defaults.
Edge reads only the physical `NodeBudget` fields from the common budget file;
`api_episode_slots` is a worker job quota, and inflight/RPM/TPM limits belong to
its separate API quota. `dsec-host` passes this file through `--node-budget`.
A standalone sandbox daemon can use a physical-only budget JSON, including
`network_interface` and optionally `disk_path`/`disk_device` for host sampling.
Formal scheduled workers require the service's `node-admission-v1` capability.

Edge persists one lease per physical sandbox under
`state_root/sandboxes/node-leases`, before VMM/container or host handle allocation.
Direct SDK creates, multiple workers connected to this Edge, baseline forks and
ready-pool checkout share this admission authority. Checkout transfers the ready
VM's existing lease and admits only the reservation increase; it does not reserve
another copy of its memory or disk. TTL cleanup releases the physical lease
without waiting for a worker receipt. Cleanup failure or uncertain ownership
retains the lease. Worker restart restores job slots without adding node leases.

Virtual guest memory limits, physical admission estimates and measured PSS are
separate quantities. The default active/create reservation is 1 CPU, 512 MiB
memory, 1024 MiB disk and 1 Mbps network; this does not change a manifest's guest
memory limit. Optional `scheduler.node_default_demand` and `node_ready_demand`
accept the `NodeDemand` fields (`cpu`, `memory_mb`, `disk_mb`, `network_mbps`,
`disk_io_mbps`) to configure these estimates. Each override must provide the
first four fields; `disk_io_mbps` is optional. By default ready demand keeps the
active memory/disk estimate, reduces CPU to at most 0.05 and sets network/disk-I/O
to zero. Pool boot reserves the maximum of create and ready estimates; checkout
must acquire the active increase. These defaults are provisional estimates,
not a measured density claim or hard cgroup enforcement, and need real-host
calibration. Whole-host pressure floors still apply, including external loads.

The node scope is **one Edge instance**, not all independent instances on a host.
Partition host budgets between independent Edge instances. `node_status` exposes
the authoritative scope, budget, reservations, live lease identities and host
sample; `scheduler_status.resource_scopes` derives its node view from Edge.
Workers connected to the same Edge repeat that global node view: do not sum their
physical reservation metrics. Job/API views remain worker-local. Multiple workers
do not automatically share a provider-account limit; partition that limit between
them. Sharing an API quota object coordinates schedulers within one process only;
restarting that process resets its rate window. Live partial budget replacement
is not supported.

Only `NodeAdmissionBusy` proves that creation had no sandbox effects. A scheduled
worker records its reasons and elapsed waiting time, and retries the same stable
request ID. A direct SDK caller receives that error and chooses when to retry.
UNKNOWN, transport failures and uncertain resource commits are not replayed.
For shell execution and stop, `busy-nonadmission-v1` additionally guarantees that
`ServiceBusy` from operation-lock contention leaves no admitted journal intent
or effects. The SDK may wait and retry with the same supplied request ID only
when the service advertises that capability. Explicit-ID calls against older
services keep their original behavior; UNKNOWN never becomes retryable.
Existing request IDs, argument digests and lifecycle journals remain unchanged;
resource hints are an optional create envelope and must also match on retries.

For an upgrade, stop the old worker, restart Edge using the same owned instance
roots and new budget, then restart the worker. Edge conservatively adopts existing
VM/container records; missing registry entries or unavailable Docker do not prove
absence. Preserve existing journals. R3 verified a running and a paused old VM,
and an old-client-created container, using their original durable records.
Check directory ownership and permissions before upgrading: the host rejects
unexpected shared access. For example, a legacy-created parent inheriting 0775
requires an operator-reviewed permission correction; the service does not
silently change arbitrary existing directories. Retain the distinct OverlayBD
2710/group-search policy described above.

Queued API calls consume neither rate tokens nor concurrency until dispatched.
Calls already attempted retain their RPM/estimated TPM reservation if cancelled
or their result is unknown. API completion, cancellation or scoring never releases
the sandbox's node lease: confirmed resource cleanup remains the release boundary.

## Commands and acceptance

`init`, `validate`, `doctor`, `render`, `render-privileges`, `run`, `wait`, `status` and `smoke` are
installed commands. `doctor` checks host access and free-disk prerequisites;
`doctor --live` also checks service health. `wait` handles asynchronous startup.
Rendered units are not installed or started automatically, and rendering refuses
to replace differing files. See [README](../../README.md) for the executable sequence.

`smoke` uses a non-TB task and saves dialogue, version, rollout identity and
cleanup evidence. Its write command fails if accidentally executed a second
time. `--restart-services` additionally checks live process identity and paused
recovery on a dedicated instance. All owned sandboxes are stopped afterwards;
failed checks also save evidence. This is deterministic lifecycle acceptance,
not model quality evaluation or RL training.

For a catalogued networked instance, prepare administrator material with:

```sh
.venv/bin/dsec-host --config host.json render-privileges \
  --user runtime-user --slot-offset 4096 --out reviewed-privileges
# Inspect all generated files. Select an address range disjoint from other instances.
sudo python3 reviewed-privileges/install-privileges.py
sudo python3 reviewed-privileges/install-privileges.py --apply
```

This renders an instance-specific helper, immutable configuration, sudoers rule,
and `host-with-network.json`. The root installer verifies embedded SHA-256 pins,
validates sudoers, refuses differing existing installations, and never imports
runtime or experiment code. It grants the named account only the helper's fixed
operation set. The helper pins ordinary and optional DAX VMM builds, bounds the
assigned address slots, and launches the VMM after dropping to the runtime UID.
Rendering grants no privileges. Re-render services using `host-with-network.json`
after installation. Host forwarding, required network tools, and the storage
service's write scope must already be provisioned; this installer does not
change them, replace another helper or restart any existing services.

Fixed task trajectories can be checked with the installed interpreter in
isolated mode:

```sh
.venv/bin/python -I tools/verify_installed_task.py --config host.json \
  --trajectory tools/fixtures/tb21-openssl.json --expect-score 1 --out tb21-acceptance.json
```

The fixture pins the TB2.1 source revision. This command saves raw observations,
official verdict and cleanup status; it is functional acceptance, not model
evaluation. The underlying worker saves canonical verifier files separately.

An isolated instance with two slots and resources for a baseline plus one branch
can verify prepared-state reuse between episodes:

```sh
.venv/bin/python -I tools/verify_installed_fork.py --config host.json \
  --trajectory tools/fixtures/tb21-openssl.json --out fork-acceptance.json \
  --restart-services
```

This uses a shared immutable prepared disk and inherited application memory,
starts two successive fresh policy histories, checks private writes and a real
official verdict, then deletes the source while a branch remains alive and
checks branch pause/resume and final backing reclamation. The optional restart
affects only the services named in this configuration. Run on an idle isolated
instance; it is not a parallel fork performance test.

v0.1 targets a trusted single-host runtime user. Private sockets, scoped helpers
and leases do not isolate mutually hostile host users. Fresh-host helper
provisioning, provenance and license review, whole-backend comparison and the
remaining release gates are tracked in [release plan](../../ROADMAP.md).


### Edge ownership during restart

The durable Edge takes an exclusive lock on its instance directory before
creation or recovery. A second owner is rejected. Graceful service `detach`
persists the existing records, retires control threads and closes local process
handles while preserving registered sandboxes for the next Edge. Old Python
objects lose authority to execute, create or stop sandboxes after this handoff.
Explicit Edge `close` instead stops sandboxes and releases ownership only after
cleanup succeeds; failed cleanup retains ownership for a retry. This is one
instance's directory ownership, not a global lock or budget across host instances.
