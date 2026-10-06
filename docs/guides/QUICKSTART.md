# Single-host v0.1 deployment

The supported release scope is a trusted runtime user on Linux x86_64 with
systemd user services, cgroup v2 and accessible KVM. The control plane is
installed independently of benchmark datasets, model servers and optional
storage services. It is an independent DSec reproduction.

## 1. Install and select external artifacts

```sh
python3 -m venv .venv
.venv/bin/python -m pip install ./dsec_reproduce-0.1.0.dev0-py3-none-any.whl
.venv/bin/dsec-host --help
```

Use Python 3.11 or newer. Offline installations use `--no-index --no-deps` on
the local wheel; the core has no third-party Python runtime dependencies.
Source builds require setuptools 77 or newer and wheel. Do not copy a venv
between machines: install into a new one.

| Component | Required for | Provisioning boundary |
| --- | --- | --- |
| Firecracker and guest kernel | Any microVM | Supply trusted binaries with known hashes; the experimental release used Firecracker 1.17.0 and a 6.1.186 kernel |
| Guest filesystem and command agent | Any task | Build the small smoke guest below, or supply the selected environment's prepared recipe |
| KVM access and `kvm` group | Any microVM | Administrator grants the runtime user device access once; `sg` only refreshes existing membership |
| Docker, GCC/static libc, e2fsprogs | Building the smoke guest | Build-time tools; Docker is not required to run an already prepared file-ext4 guest |
| EROFS-capable kernel, static BusyBox and mkfs tools | Layered image preparation | Explicit build inputs; required applets include mount/chroot and ip for networked guests |
| Instance network helper | Networked guests | Administrator installs the generated, reviewed helper; forwarding and ip/iptables tools must be provisioned |
| OverlayBD/ublk service and device rules | OverlayBD roots | External root service must grant the chosen storage paths and device group access; use `UMask=0007` so runtime directories remain recoverable by the service group |
| 3FS service and client mount | `threefs_lazy` storage | Optional external deployment; not needed for local storage |

No install command downloads guest kernels, images, models, datasets or storage
binaries. Record their source revisions, licenses and SHA-256 pins. The optional
network installer is not a general-purpose root installer for these services.
The initial deployment can use a networkless file-ext4 guest and add capabilities
afterwards; it does not need a training framework or a TB2.1 installation.

## 2. Build a fresh non-TB smoke guest

Use a trusted, locally available Linux image containing `/bin/sh` and mount
utilities. Resolve its exact Docker image ID first; pull/import is an explicit
administrator or build-user preparation step. The tool exports trusted image
contents, compiles our static agent and creates a 64 MiB ext4 template.

```sh
python3 tools/build_smoke_guest.py --image sha256:<local-image-id> \
  --agent-source guest_agent.c --out ./artifacts/smoke.ext4
```

The builder launches no VM, has no network use, and refuses to replace a
template. It requires static compilation support. Keep the template immutable;
runtime instances use private copies.

## 3. Configure an independent instance

```sh
.venv/bin/dsec-host --config host.json init --instance local \
  --state-root "$HOME/dsec-state" --network-interface eth0 \
  --binary /opt/dsec/firecracker --kernel /opt/dsec/vmlinux \
  --template ./artifacts/smoke.ext4
.venv/bin/dsec-host --config host.json doctor
.venv/bin/dsec-host --config host.json run sandbox --validate-only
.venv/bin/dsec-host --config host.json render --out rendered-units
systemd-analyze --user verify rendered-units/*.service
```

Use the actual host interface and existing artifact paths. Example budgets are
small admission budgets, not a maximum host capacity. The user must own the
state directory, be in the configured service group and be able to traverse
parents. Keep the state root short enough for Linux Unix sockets.

Inspect the rendered units. Install them under your own user service manager:

```sh
mkdir -p "$HOME/.config/systemd/user"
cp rendered-units/dsec-local-*.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user start dsec-local-sandbox.service dsec-local-worker.service
.venv/bin/dsec-host --config host.json wait
.venv/bin/dsec-host --config host.json doctor --live
.venv/bin/dsec-host --config host.json smoke --restart-services --out acceptance.json
```

Use an unused instance name and run restart acceptance on an isolated instance.
The smoke records its result and releases its sandbox. Check `status` for an
empty queue before stopping the two services. Stopping the sandbox service
preserves VMMs for recovery; it is not a substitute for stopping owned sandboxes.

## 4. Add a task application or optional storage

The [TB2.1 application](../../apps/tb21/README.md) is an optional, separately installed
package. Prepare only the selected tasks and artifacts, register a pinned
environment catalog, then configure the dedicated instance. Installing the
core does not prepare TB2.1 or require that application package.

For networked/OverlayBD/3FS/DAX recipes, use the explicit fields in
[host configuration](DSEC_HOST_CONFIGURATION.md). The root network helper,
storage group's directory traversal and storage service's systemd write scope
are separate requirements. Do not use world-writable directories or give the
worker arbitrary sudo to bypass them.

An optional `dsec-artifact-integrity seal` command publishes an fs-verity
receipt for an admin-owned immutable artifact. It requires Linux and a
verity-capable filesystem. Publication builds the kernel's Merkle tree and
checks the existing whole-file SHA-256; runtime measures its protected digest
and the kernel checks pages as they are read. Receipt and artifact paths,
including ancestors, must be root-owned and not writable by other users.
The original full-hash mode remains available. Selecting fs-verity explicitly
fails if a receipt or kernel protection is missing; it never silently skips
verification. See [artifact publication](ARTIFACT_PUBLICATION.md) for the
configuration and current validation boundary.
The installed r5 candidate passed real write rejection, unsealed-copy rejection,
corrupt-page read rejection and 15 fixed-trajectory task scores. This integration
currently covers the local verifier disk; 3FS and DAX are outside that validation.

## Verified scope

The release has installed-runtime, non-TB, representative TB2.1, GRPO,
restart/reuse and resource-boundary evidence. A new smoke guest can be built
from source and checked independently of those TB images. The experiment
machine's existing KVM, VMM/kernel and optional storage deployments have been
reused; this is not a claim that every optional dependency was installed from
zero on a second physical host. See the [acceptance record](../reports/DSEC_V01_INSTALL_ACCEPTANCE.md).
The latest installed candidate's grouped training and prepared-state isolation
are recorded separately in [r5 GRPO acceptance](../reports/DSEC_V01_GRPO_ACCEPTANCE.md).
