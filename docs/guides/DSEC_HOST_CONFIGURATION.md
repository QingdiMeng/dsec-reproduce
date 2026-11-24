# Installed single-host configuration

`dsec-host` is the wheel's configuration and acceptance entry point. The core
installs independently; task plugins are optional and installed when used. Images,
task definitions, kernel, Firecracker and storage services remain explicit artifacts.
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
journals. `DevEdgeClient` is the explicit node-local development SDK; its caller needs the service
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

A 3FS deployment must provision matching server/client binaries and their
dynamic libraries, the configured RDMA device, listener NIC/address, and a live
`fuse.hf3fs` source mount readable by the runtime user. Keep a reproducible runtime
dependency set: check the restored executables
with `ldd` inside the intended runtime image before starting services, and pin
required library packages in that image or an explicitly versioned library mount.
Do not rely on packages installed only in an existing container's writable layer.
Publishing to that store
also needs a separately writable artifact directory; `allow_other` alone does
not grant publication rights. Keep FUSE propagation scoped to the dedicated
storage subtree and avoid overlapping recursive parent binds. Core installation
does not provision or repair this external cluster. R3 exercised one real 3FS
EROFS layer through two VMs, including private-write isolation and pause/restore;
that functional check is not a distributed-storage or cold-read benchmark.

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

`init`, `validate`, `doctor`, `preflight`, `prepare-node`, `render`, `render-privileges`, `run`, `wait`, `status` and `smoke` are
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

For a catalogued microVM Node, run as the actual unprivileged service account.
Use the Node configuration generated by the cluster deployment workflow:

Administrator installation is a one-time step for each instance, with an explicit
reviewed update when its pinned identity, paths, VMM or address allocation changes.
Normal sandbox operations invoke only the installed fixed helper noninteractively;
operators should not run installation commands for each sandbox or episode.
Do not grant the runtime account unrestricted passwordless sudo. Retiring an
installation requires administrator cleanup after all owned resources are stopped.

For networked execution, select a boot artifact built by `dsec_image.boot` with
`--network`. Creating a TAP and passing guest network parameters does not configure
an older networkless boot artifact. Initialization never rewrites shared images;
acceptance must use a separate prepared artifact that matches the declared runtime.

```sh
.venv/bin/dsec-host --config host.json preflight --node-config node.json
.venv/bin/dsec-host --config host.json prepare-node --node-config node.json \
  --user runtime-user --slot-offset 4096 --out reviewed-privileges
# Inspect all generated files. Select an address range disjoint from other instances.
sudo /usr/bin/python3 reviewed-privileges/install-privileges.py
sudo /usr/bin/python3 reviewed-privileges/install-privileges.py --apply
.venv/bin/dsec-host --config reviewed-privileges/host-with-network.json \
  preflight --node-config node.json
```

`preflight` reads host prerequisites without executing the network helper,
creating state directories, connecting to the control plane or starting services.
It returns `READY`, `NEEDS_INITIALIZATION` (the instance helper/configuration is
missing), `ADMIN_CHECK_REQUIRED`, or `BLOCKED`. Ordinary accounts may lack ptrace
permission to read PID 1 namespaces; this is an unverified condition, not proof
of a mismatch. The installer must verify it as administrator, and the existing
runtime helper checks context before each operation. A confirmed namespace
mismatch blocks immediately. Resource-pressure checks carry `RESOURCE_WAIT` within a
blocked report. Only `READY` exits zero. `prepare-node` permits the missing-helper
and administrator-verification cases,
rejects blockers before creating output, and binds advertised environments,
minimum resource demands and Edge/worker sockets to this Host configuration.
It does not register the Node or grant permissions.

Checks include Linux, real KVM/TUN character-device identity and read-only
ioctls, capability bounds, privilege escalation restrictions, user/namespace
context, forwarding, tools, the runtime account's KVM group, artifacts, storage
socket permissions and current memory/disk floors. Memory headroom is capped by
the visible cgroup v2 memory limit. A container marker is diagnostic, not a
provider-name rejection rule: restricted Pods are rejected for their missing
capabilities/devices. Missing host capabilities, remapped user namespaces or
`NoNewPrivs` cannot be repaired by this initializer. Have the host administrator
or provider provision them, or select another Node. Account/group provisioning,
package installation, kernel modules and storage-service setup remain explicit
administrator tasks; the command does not change Docker, forwarding or global
network rules.

`READY` is a prerequisite observation, not a VM boot or isolation attestation.
The read-only checks cannot prove every seccomp/AppArmor policy, real namespace
operation, sudo execution or storage-service health. Explicit service startup
and runtime acceptance are still required before admitting production work.

This renders an instance-specific helper, immutable configuration, sudoers rule,
and `host-with-network.json`. The root installer verifies embedded SHA-256 pins,
validates sudoers, refuses differing existing installations, and never imports
runtime or experiment code. Before installation-target changes it rechecks the
live platform using an embedded standard-library-only probe, checks the pinned
runtime UID/KVM GID, and repeats device access checks after dropping a child
process to that runtime identity. It never treats a saved preflight report as
authorization. It grants the named account only the helper's fixed
operation set. The helper pins ordinary and optional DAX VMM builds, bounds the
assigned address slots, and launches the VMM after dropping to the runtime UID.
Rendering grants no privileges. Re-render services using `host-with-network.json`
after installation. Host forwarding, required network tools, and the storage
service's write scope must already be provisioned; this installer does not
change them, replace another helper or restart any existing services.
The lower-level `render-privileges` command remains available for existing
workflows; its generated installer has the same fresh platform guard.

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

### Native sessions/files/streaming: development opt-in

This iteration's Python SDK and shared C agent passed local UDS and single-host
Linux acceptance, including vsock, idle-session snapshots, layered containers
and restart/crash recovery. Deployment remains an explicit opt-in for newly
prepared artifacts. See the [native SDK contract](../architecture/AGENT_ENVIRONMENT_CONTRACT.md)
for timeout, cancellation, binary files and reconnect semantics, and the
[installation report](../reports/DSEC_V01_INSTALL_ACCEPTANCE.md) for the tested scope.

Upgrade guest artifacts explicitly; changing the Python wheel cannot add a
native listener to an already running old guest. Build a new microVM template
with `tools/build_smoke_guest.py` and the updated `guest_agent.c`, keeping
`guest_native.c` next to it. Keep the old template/catalog for existing guests;
register the new immutable artifact as a separate environment for acceptance.

For a new layered-container instance, build the same agent as a static Linux
binary from the source distribution:

```sh
python tools/build_native_agent.py --source guest_native.c --out "$ARTIFACTS/native-agent-v1"
export DSEC_NATIVE_AGENT="$ARTIFACTS/native-agent-v1"
```

Set this environment variable on Edge together with the normal
`DSEC_CONTAINER_AGENT` pointing to the updated container agent. The backend
mounts the native binary read-only; the container supervisor starts it after
mounting the private guest root and stops it before unmounting. The private
UDS is inside that instance directory. Native commands and file paths resolve
inside the same guest root as the existing one-shot shell path.
The complete host path `<DSEC_CONTAINER_ROOT>/<32-character-id>/native.sock`
must fit Linux's 107-byte pathname limit. Edge rejects a longer path before
creating a sandbox; shorten the configured root instead of retrying the call.
Absent this opt-in, old container startup and commands retain their existing
behavior; calling a native API reports `UnsupportedCapability`.
This does not upgrade legacy TB2 OpenEnv server images or install benchmarks,
GPU trainers, Rust storage binaries, or system-wide services.

After deploying a new immutable guest, verify it through the installed SDK:

```sh
python -I tools/verify_native_sdk.py --socket "$STATE/sandboxes/service.sock" \
  --environment python-native --out native-sdk-acceptance.json
```

For a prepared layered container, add `--backend container` and its environment
ID. This tool creates at most two sandboxes, saves failure evidence, and stops
them on exit. It checks session state, binary transfers, deduplication, overlap,
stream reconnection, cancellation and timeout bounds; microVMs additionally
check active-pause rejection and idle-session snapshot restore. Restart/crash
acceptance requires a dedicated Edge instance and is a separate host procedure.
Match the guest's network recipe to the service configuration: a boot artifact
built with `--network` requires a configured guest NIC, even when a particular
task or verifier itself needs no Internet access.

## Networkless QEMU Container and FnCall

These Linux execution previews require an **Edge contract 2** release.
Automatic upgrade or rollback from
contract 1 rejects because the old runtime cannot own these persisted instances;
drain and provision a fresh instance, with a reviewed migration if old state must
be retained. Keep Node gateway, Edge and any configured episode service on the
same wheel. Old ordinary Container request/digest bytes are preserved.

The runtime requires unprivileged read/write
access to `/dev/kvm` and `/dev/vhost-vsock`, QEMU/KVM, and prepared artifacts.
They do not install host Docker, change bridges/firewall rules, or provide
outbound networking. [The isolation contract](../architecture/EXECUTION_ISOLATION.md)
lists remaining production gates. Use a dedicated new Container state directory;
do not point it at a legacy host-Docker instance or another Edge's state.

Prepare templates on a Linux build machine with Docker and a static C compiler.
Supply exact already-local image IDs, not mutable tags. The trusted base needs
Python 3, `/bin/sh`, mount/mountpoint and coreutils; the task image needs Python 3
and `/bin/sh`. A pinned Docker dind image supplies static Docker binaries, but
the builder only exports its stopped container; it never runs dind on the host.

```bash
python tools/build_docker_guest.py --base sha256:BASE_IMAGE_ID \
  --docker-tools sha256:DOCKER_TOOLS_IMAGE_ID --task-image sha256:TASK_IMAGE_ID \
  --source . --out /srv/dsec/artifacts/function.ext4 --mode fncall
python tools/build_docker_guest.py --base sha256:BASE_IMAGE_ID \
  --docker-tools sha256:DOCKER_TOOLS_IMAGE_ID --task-image sha256:TASK_IMAGE_ID \
  --source . --out /srv/dsec/artifacts/container.ext4 --mode container
```

Replace the symbolic IDs with 64 lowercase hex characters. Preserve both output
records: `sha256` pins the template; `inner_image` pins the image configuration
that inner Docker actually loads. With a containerd-backed host Docker, its
manifest ID can differ from that configuration ID. The builder derives the
correct value from the pinned exported archive; do not substitute a tag or
blindly reuse the host's image ID.

Prepare an absolute mode-0700 runtime-owned shared CID pool directory. Cooperating
Edges on one host must use the same ledger and range, disjoint from other VMMs.
Create a trusted `schema:2` catalog with `cid_pool:{root,first,last}` and an
`environments` map. Each environment has these required fields:

| Field | Required value |
| --- | --- |
| `mode` | `fncall` or `container`, matching its prepared template |
| `binary`, `kernel`, `template` | Objects containing canonical absolute `path` and `sha256` |
| `image` | The builder's `inner_image` |
| `memory_mb`, `cpu_count` | Outer VM RAM and vCPUs, e.g. 1024 and 1 |
| `inner_memory_mb`, `inner_cpus` | Prepared inner limits, currently 512 and 1 with the CLI defaults |

Add `sandbox.qemu_execution_catalog` to the Host configuration. Optionally set
`worker.container_root` to the new private state directory; otherwise Edge uses
`state_root/containers`. This catalog is exclusive: absent environments and
unsupported modes/storage/QoS fail before create; there is no host-Docker fallback.
Artifact contents are verified before boot. Editing a catalog requires an
explicit Edge restart. Do not change catalog contents for a live instance.

Rebuild Container/FnCall templates with the current builder. Catalog schema 1
and old unauthenticated templates are rejected; drain their instances using the
matching old release before switching to fresh state. Changing only the schema
number cannot upgrade an old guest. The kernel needs `CONFIG_VIRTIO_CONSOLE=y`.
Edge creates a unique mode-0600 `channel.key` in each private instance directory
and injects it through a dedicated virtio-serial bootstrap socket. Do not copy
keys between instances or publish them with logs. Keep this file with private
instance state across Edge restarts: a missing/changed key fails closed, never
generates a replacement for a live VM. New VM creation supplies key rotation;
live rekey is unsupported. Keys remain in private stopped records for recovery,
and can be removed when the entire retired instance is deleted.

The Node advertises route backends `microvm` and `container`, with distinct
Firecracker and QEMU environment IDs. Add both sets to `node.environments` and
`minimum_demands`. Every QEMU minimum must include one concurrency slot, all outer
RAM/vCPUs, and the full private raw disk size (2048 MiB by default), even though
the inner container limit is 512 MiB. `node-preflight` checks this; Node and Edge
also enforce observed minima during API creation. FnCall's actual prepared
capability is published from Edge health, not a caller-supplied configuration bit.

Public clients use the existing HTTPS API and project credentials:

```python
from dsec.sdk import DSecClient, DSecFnCallRunArgs, DSecContainerRunArgs

async with DSecClient(["https://api.example"], token=token,
                     project="research", ca_file="ca.crt") as client:
    demand = dict(cpu=1, memory_mb=1024, disk_mb=2048, network_mbps=0)
    slot = await client.run_fncall(
        DSecFnCallRunArgs(environment_id="python-function"), resource_demand=demand)
    try:
        first = await slot.invoke("echo 42 >/work/private; cat /work/private")
        fresh = await slot.invoke("test ! -e /work/private")
    finally:
        await slot.stop()
    box = await client.run_container(
        DSecContainerRunArgs(environment_id="python-container"), resource_demand=demand)
    try:
        await box.run_shell("echo persistent >/work/value")
        result = await box.run_shell("cat /work/value")
    finally:
        await box.stop()
```

Each function slot holds its quota while warm. `attach_fncall` reconnects without
creating another slot; persistent shell/file sessions belong to Container and
MicroVM, not FnCall. Choose stable request IDs when a result may need recovery;
query UNKNOWN instead of reissuing the invocation under a fresh ID. Successful
function replies follow process/tmpfs cleanup, and failed cleanup retires the VM.

### Direct Linux FullVM preview

On a prepared Linux KVM Node, build a pinned standalone guest. The build
uses Docker only to export the trusted local base root filesystem; there is
no Docker daemon inside the FullVM.

```bash
python tools/build_docker_guest.py --mode fullvm --base sha256:BASE_IMAGE_ID \
  --source . --out /srv/dsec/artifacts/fullvm.ext4 --disk-mb 512
```

Use a Debian-compatible trusted base with Python 3, `setpriv` and standard
mount/core utilities. The kernel must support the same QEMU virtio-blk,
vsock and virtio-console devices used for Container/FnCall. In the QEMU
catalog, set `mode: fullvm`, pin `binary`, `kernel` and `template`, and set
`memory_mb` and `cpu_count`. Omit the inner Docker image and resource fields.
This preview accepts a private local disk and no network. Register the
environment on the Node with route backend `fullvm` and minimum demand
including the entire VM and private disk. Use a new, private Edge instance
when evaluating it; a prior Edge cannot open a catalog with FullVM entries.

```python
from dsec.sdk import DSecFullVMRunArgs

box = await client.run_fullvm(
    DSecFullVMRunArgs(environment_id="linux-fullvm", memory_limit_mb=512, cpu_cores_limit=1),
    resource_demand=dict(cpu=1, memory_mb=512, disk_mb=512, network_mbps=0),
)
try:
    print(await box.run_shell("id; uname -a"))
finally:
    await box.stop()
```

The request's CPU and memory must match the environment's fixed outer VM
limits; the physical demand must also cover any configured host overhead.
The guest supervisor authenticates vsock using the per-VM key. Commands
and native sessions run as guest UID 1000, with no new privileges. Status
reports `backend=fullvm`. For a scoped API check, run
`tools/verify_execution_paths.py --fullvm-only` using `paths.fullvm.spec` and
`paths.fullvm.demand` in the acceptance configuration. The default environment
has no display; Windows and snapshot/restore remain unsupported.

To opt in to the networkless desktop preview:

- Use a pinned guest kernel with virtio GPU/DRM, virtio input, evdev, and
  the normal FullVM virtio-blk, console and vsock drivers (built-in or
  loaded by the guest before the authenticated service starts).
- Add the desktop packages and a trusted executable `/opt/dsec-desktop/start`
  to the base root filesystem. The builder's `--desktop` flag runs that hook
  after guest mounts and before starting the native service. The hook must
  return after starting a desktop; it is part of the pinned image.
- For Xfce tasks that manipulate files through the desktop, include a file
  manager that provides the session D-Bus service, such as Thunar. Xfdesktop
  can display icons and the Rename menu even when that service is missing.
- Set `"display": "virtio-2d"` on the FullVM catalog entry. Use an isolated
  preview Edge without a QEMU host confinement profile; the desktop path
  has not been qualified with that profile.

`await box.screenshot()` returns PNG bytes and dimensions over the same
authenticated API. `await box.desktop_input(events, request_id=...)` accepts
up to 64 bounded key, button or absolute pointer events (coordinates 0–32767).
For mouse clicks on the virtio tablet, send button-down and button-up in
separate calls with distinct request IDs. A single batch containing both can
return successfully without producing a visible click in the guest. When
moving and clicking, send the absolute pointer position before button-down.
Use `box.query_request(id)` if input may have been delivered before the
reply was lost. No remote VNC port, guest TCP listener, GPU passthrough or
accelerated graphics backend is required. The Ubuntu test image has an Xfce
desktop and an xterm named `DSecDesktop`; use the existing
`tools/verify_execution_paths.py --desktop-only` test with that fixture.


`tools/verify_execution_paths.py --config PATH --out REPORT` runs installed SDK
acceptance through HTTPS, with at most two simultaneous sandboxes and automatic
stop attempts. Its JSON configuration contains `client` (the DSecClient keyword
arguments except token), `token_file` (mode 0600), and `paths` keyed by `fncall`,
`container`, `microvm`; each path has `spec` and `demand` as in the example.
The checks cover function freshness/dedup/timeout, Container persistent sessions
and binary files, distinct CIDs/workspaces, and Firecracker pause/resume. Keep
failure reports and check physical/IAM/CID leases after the run. These checks do
not prove tenant security, cold-start performance, image sharing or whole-host HA.

### QEMU host confinement

This opt-in path passed bounded networkless Linux AppArmor/VMM acceptance;
broader production security and networking gates remain open. See the
[acceptance report](../reports/DSEC_V01_INSTALL_ACCEPTANCE.md).
Add this top-level field to the schema-2 QEMU catalog,
using the actual runtime UID, instance name and service cgroup path:

```json
"host_isolation": {
  "cgroup_root": "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/dsec-example-sandbox.service",
  "helper": "/usr/local/libexec/dsec-example-qemu-profile",
  "memory_overhead_mb": 256,
  "pids_max": 128
}
```

The broker binds one shared QEMU binary and, for direct boot, one kernel.
Use separate Nodes for different binaries or direct-boot kernels. Offline UEFI
FullVMs declare firmware and QCOW2 files in the catalog. The renderer pins
the exact read-only firmware and backing files for the administrator to approve.
Each VM receives write access to its own QCOW2 overlay and UEFI variables.
Start with a fresh
drained Container state directory: policy is part of the immutable catalog and
live VM identity, not a setting to switch underneath running instances.

Render a reviewable, pinned administrator installation as the service account:

```bash
dsec-host --config host.json render-qemu-privileges --user dsec --out qemu-privileges
apparmor_parser -Q -K qemu-privileges/sample.profile
# Administrator: review and check, then install once.
sudo python3 -I qemu-privileges/install-qemu-privileges.py
sudo python3 -I qemu-privileges/install-qemu-privileges.py --apply
dsec-host --config host.json render --out user-units
```

The installer reuses the restricted installer checks, refuses different existing
targets, and installs only the fixed AppArmor helper/config/sudoers. It does not
change Docker, networking, cgroups, services or modules. Runtime calls to this
helper use noninteractive sudo; no shell/root launcher or arbitrary profile/path
argument is accepted. Keep Edge's own sudo capability; NoNewPrivs is set in the
VMM child, not the helper-using Edge. The host must expose fine-grained AppArmor
Unix socket mediation (`/sys/kernel/security/apparmor/features/network/af_unix`
reports `yes`). Loading a syntactically valid profile alone does not prove VMM
startup or its access boundaries; validate both on the deployment host.

The generated sandbox unit uses `Delegate=cpu memory pids` and
`DelegateSubgroup=edge` (systemd 254+). Confirm its actual `ControlGroup` matches
the catalog. SSH foreground execution outside that delegated service rejects.
Each VM gets a child group with `memory.max = guest RAM + overhead`,
`memory.swap.max=0`, `memory.oom.group=1`, bounded `pids.max`, and CPU quota
matching vCPU count. Update Node minima **and client demand** to include overhead:
1024 MiB guest plus 256 MiB overhead requires at least 1280 MiB admission.
Unconfigured development catalogs retain their previous minimum; enabling host
confinement must not silently hide its extra memory cost.

For a bounded controller-only check, run `tools/verify_host_cgroup.py` in a new
dedicated delegated service with its own cgroup root and report path. It tests a
48 MiB child OOM, process refusal and CPU throttling, then removes its empty
groups. It is separate from actual QEMU/AppArmor, tenant or networking acceptance.

### Isolated QEMU IPv4 egress (bounded Linux acceptance)

Keep existing environments networkless unless networking is explicitly prepared.
For a fresh drained instance, add a catalog-level `network` object and set only
the intended environment entries to `"network": "isolated"`:

```json
"network": {
  "helper": "/usr/local/libexec/dsec-example-qemu-network",
  "uplink": "eth0",
  "slot_offset": 1024,
  "max_slots": 2,
  "policy": {"dns": "1.1.1.1", "egress_proxy": null}
}
```

`host_isolation` is mandatory. The administrator reserves a disjoint range in
`10.232.0.0/16` (one /31 per slot); overlapping existing routes reject creation.
Host IPv4 forwarding must already be provisioned. No shared bridge or host
Docker daemon is created. The guest's address/MAC can repeat because each TAP
belongs to a separate network namespace. QEMU receives one already-open TAP FD
and stays unprivileged; it does not enter that namespace or receive CAP_NET_ADMIN.

Rebuild each selected template with `tools/build_docker_guest.py --network-policy
policy.json` in addition to its existing arguments; `policy.json` must contain
the exact catalog `network.policy`. Use the rebuilt artifact SHA-256 in the
catalog. Guest init configures IPv4 before the task starts. Inner Docker uses
the outer guest's network, with capabilities dropped and no host mounts/devices.
The existing authenticated vsock control path is unchanged. A configured proxy
is one `{ "ip": "192.168.0.104", "port": 18888 }` TCP exception; it supplies
HTTP(S)_PROXY inside the task. No proxy credential is accepted here.

`render-qemu-privileges --clang /path/to/clang` builds the TC eBPF object as the
ordinary service user (Linux x86_64 headers and a BPF-capable Clang are needed
at build time). Review `egress.c`, `sample.profile`, both helpers and configs.
The single installer pins and installs six fixed root files and creates the
two private state directories. Installation itself does not change networking;
normal SDK creation invokes only the fixed broker operations for that instance.
Runtime does not compile code or accept a replacement eBPF object from callers.
Different existing installations still reject replacement; use a fresh instance
for this preview rather than overwriting running helpers or catalogs.

Adding an environment with the same installed privileges does not require
replacing helpers. Drain the Node, preserve the previous immutable catalog and
add `"catalog_history": [{"path": "/srv/dsec/catalog-v1.json", "sha256": "..."}]`
to the new catalog before explicitly restarting Edge. Historical instances are
accepted only where that environment definition, CID pool and host/network
policy are identical. Changing an old image, resource limit or policy is not an
additive update. Preserve environments still needed by retained instances;
history is explicit, bounded to 16 files and checked for content drift.

Prepared environments can set `startup_timeout_s` from 1 to 240 seconds
(default 15) for guest initialization, image import and authenticated readiness.
The QMP readiness deadline remains 15 seconds and tool-command limits do not
change. This budget is pinned in each instance record; old records use 15.
Changing it creates a new environment version, rather than reinterpreting old
instances. It is a startup allowance, not a measured creation-latency promise.

An environment may set `"disk_cache": "direct"` for its private QEMU root disk.
This requests Direct I/O on both block nodes, bypassing the host file cache
while keeping flushes enabled. It prevents a second dirty/writeback copy of
guest disk data from consuming the VMM's cgroup allowance. The default remains
`buffered` for existing environments/records; changing modes requires a new
environment version. Unsupported Direct I/O fails startup rather than falling
back. This is not DAX or shared guest memory, and it does not reduce guest RAM.
Before retiring an owned, empty cgroup, Edge saves available peak, OOM, memory,
CPU and I/O counters to `host-resources-retired.json` in the instance directory.
Missing kernel counters stay absent, not zero. This preserves failure evidence
after kernel resource removal without weakening any retirement fence.

TC guards load before any link is activated. The first policy admits IPv4
TCP/UDP public egress, exact DNS and optional proxy exceptions, and only the ARP
needed by the fixed gateway. It rejects source spoofing, IPv6, VLAN, IP options,
fragments, metadata and other private/reserved destinations. Return traffic is
conntracked. This is not bandwidth shaping, live policy rotation or IPv6 support.
Public API `network_rules` overrides remain unsupported; the environment owns
the immutable policy. Network status includes the observed kernel program
identity. Adoption refuses drift; interrupted creation never replays FD lending.

On the Node, the scoped helper's `test-policy SID` operation runs fixed positive
and negative packets through that instance's attached BPF program without
sending network traffic. It accepts no packet, program ID, path or repeat count.
Pair this with real guest DNS/HTTPS, host/private denial, restart and cleanup
checks before enabling networking for an application. Portable/native-C tests
and successful BPF compilation alone do not establish Linux enforcement.

On 2026-10-25, installed-wheel acceptance passed seven public SDK and 29
orchestration checks on one prepared Linux Node: actual attached-program packet
tests, Container DNS/public HTTPS, FnCall DNS, host/metadata/private connection
denial, Edge restart/adoption and failure cleanup. Every owned namespace, TAP,
profile, cgroup, CID, private writable disk and physical/IAM lease was retired;
the original host Docker/Grafana containers were unchanged. This used the fixed
DNS `192.168.0.1` and no proxy; it does not validate another Node's prerequisites,
configured proxy egress, adversarial multi-tenant use or connection floods. See
the [acceptance evidence](../reports/DSEC_V01_INSTALL_ACCEPTANCE.md).


### QEMU shared EROFS through virtio-pmem/DAX

This opt-in environment capability is distinct from `disk_cache`. Private ext4
I/O may use `direct` while immutable shared data uses pmem/DAX. No SDK changes
or model dependencies are required. Bounded installed-wheel Linux acceptance
passed for two Container VMs and a sequential FnCall slot on 2026-10-26; see the
[acceptance report](../reports/DSEC_V01_INSTALL_ACCEPTANCE.md). This does not
establish whole-VM savings or production-scale capacity. A separate bounded
[SWE-Gym mypy migration](../reports/SWEGYM_FIRST_USE.md#real-task-erofspmemdax-migration-2026-10-26)
passed real-file DAX checks and official baseline/oracle scoring.

Publish each **staged filesystem directory** with:

```bash
python tools/build_erofs_dax.py --source staged-layer --out /srv/dsec/layers/base.erofs
```

The publisher requires `mkfs.erofs` with `--tar=f` and `noinline_data` support.
It preserves numeric ownership via tar, disables compression, adds a reserved
DAX probe, and pads capacity to 128 MiB. This command does not fetch images,
convert OCI whiteouts, or preserve arbitrary filesystem xattrs; use the existing
OCI-aware publication path for whiteout conversion. Do not substitute a
compressed EROFS image. All layers are immutable regular files with no write bits.

Pass a JSON list of the returned `{path, sha256, bytes}` objects, bottom layer
first, to `tools/build_docker_guest.py --readonly-layers layers.json`, and repeat
`--shared-mount /opt/toolkit` for each directory supplied by their merged tree.
The directories may contain task workspaces or dependencies; they are bind-mounted
from the composed guest filesystem into the task container. Container writes copy
up to its VM's private ext4 upper; FnCall mounts them read-only. Mounting a shared
directory alone does not remove its old copy from the task image. To remove that
copy during publication, use `tools/split_docker_image.py` on a pinned local image
and pass its lean archive with `--task-archive` and `--task-archive-sha256` instead
of `--task-image`. These inputs are mutually exclusive. The builder validates the
archive hash before host effects and rechecks the copied input. The split tool
preserves runtime Config and tar ownership/metadata; declared volumes and
cross-partition hardlinks require separate preparation. See the
[SWE-Gym operator recipe](../../apps/swegym/README.md#move-shared-task-files-to-erofsdax)
for publication, catalog and application verification steps.

Add the builder's `readonly_layers` and `shared_mounts` arrays to the new catalog
environment. The paths are administrator-controlled, not API request parameters.
There are at most eight layers per environment, each 128 MiB–32 GiB and aligned
to 128 MiB. The total pmem capacity divided by 64 must leave at least 128 MiB of
guest RAM; this is only a metadata floor, not a workload sizing recommendation.
Private disk quota remains charged separately; shared capacity is not multiplied
into every sandbox's writable-disk reservation. Guest readiness requires actual
DAX attributes, correct device capacities and private ext4/OverlayFS mounts.

For a new Node, render its normal privilege bundle with the explicit publication
root (which must already exist):

```bash
dsec-host --config host.json render-qemu-privileges --user dsec --out qemu-privileges --layer-root /srv/dsec/layers
```

Administrator installation is required once for this capability. The broker
subsequently accepts exact per-VM read/mmap grants only inside that fixed root;
it does not grant writes or a blanket directory rule. A running instance cannot
change its layers. Claims and retirement keep the original policy identity, and
shared artifacts are never deleted by instance cleanup. Existing no-layer
profiles retain their original text and lifecycle behavior. Updating an existing
broker requires draining its VMs first and retaining the previous helper/config.

Runtime whole-file SHA-256 verification currently reads these local artifacts;
this path has **not** established cold-cache lazy-read performance. Reusing the
existing fs-verity publication receipts is required before making that claim.

### Bounded QEMU commands and recovery

Existing environments retain the 30,000 ms command ceiling. To support longer
commands, build a **new** guest with `--command-timeout-ms 120000` and set the
same `command_timeout_ms: 120000` in its new execution-catalog environment. The
maximum supported ceiling is 300,000 ms; API callers cannot raise it. Readiness
checks the guest's configured ceiling against the pinned catalog. Rebuild the
guest rather than changing only its catalog, and retain every explicitly required
historical catalog pin when appending an environment; history trust is not
transitive. Existing no-field environments remain compatible.

The caller supplies a bounded `timeout_ms` for each command. State and result
queries do not queue behind its execution lock. Client disconnection or task
cancellation does not undo an already admitted action. Save its request ID and
query `box.query_request(id)` after reconnecting instead of sending it again.
A stop that collides with active shell work is rejected as `ServiceBusy` before
admission; let the bounded command finish or time out, then stop the sandbox.
This is not an immediate per-command cancellation API.

After Edge owner loss, the host journal preserves uncertain intent. A query can
commit DONE only from the exact original VM's matching request ID/digest and
durable guest DONE response. Missing, mismatched or unavailable proof stays
UNKNOWN; no command replay or successful result is inferred from VM liveness.
On Linux, the command helper uses a subreaper boundary to retire orphaned/setsid
and background descendants on timeout or failure. Failure to prove retirement
fails the command/slot rather than reporting clean success. Successful Container
shells retain the existing ability to leave deliberate background processes;
the inner container init reaps exited orphan processes to avoid zombie buildup.
It is mounted read-only at reserved `/dsec-init`, outside task shared directories;
Docker's implicit `/sbin/docker-init` can be hidden by a shared `/usr` mount.
FnCall still resets its entire container between calls. Native sessions inherit
the same prepared command ceiling: the builder compiles that agent limit, the
host channel enforces it, and the guest proxy allows that bounded response time.
