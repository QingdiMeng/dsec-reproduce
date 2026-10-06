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
