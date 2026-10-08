# Terminal-Bench-2.1 application for DSec

This optional application is installed separately from the DSec control plane.
It contains task adapters, manifest validation, preparation code and a worker
verifier plugin, not tasks, images, model weights or an OpenEnv server. No command downloads anything implicitly. Supply a trusted checkout
and locally available images and tools explicitly.

```sh
# First install the DSec control plane, then this optional application.
python -m pip install ./apps/tb21
dsec-tb21 stage --repo /data/terminal-bench-2-1 \
  --tasks openssl-selfsigned-cert --out /data/tb21-case
dsec-tb21 check --suite /data/tb21-case
```

The checkout must be revision
`7131e4375048a0e408a8fb404b5f499d726b695b`, with 89 source tasks. Staging a
subset is supported; `--all` explicitly selects all tasks. Selected source
tasks must be clean. Staging excludes oracle `solution` directories and pins
every copied file, including instruction, configuration and official verifier.
Checks reject changed, missing or extra files, symlinks and oracle solutions.

## Use existing prepared artifacts

```sh
dsec-tb21 register --suite /data/tb21-case \
  --catalog /data/prepared-environments.json --out /data/tb21-catalog.json
```

The source catalog contains entries named `tb2-<task-id>`. Registration verifies
the selected task resources and local artifact hashes, then writes a new subset
catalog. It copies no disks and supports the core's existing file-ext4 and
OverlayBD/ublk recipes, including previously compacted or DAX recipes.
Registration never modifies running services or the source catalog.

Configure a dedicated `dsec-host` instance with the resulting catalog,
`worker.tb2_tasks_dir=/data/tb21-case/tasks`, and
`sandbox.tb2_manifest=/data/tb21-case/manifest.json`. Supply the verifier tool
disk and its manifest through the documented host options. Configure an
instance-specific network helper for networked guests. TB2.1 scoring is the
staged task's official `tests/test.sh`; verifier outputs are saved by the worker.
Install this application in both the sandbox daemon's and worker's Python
environments, using the same checkout as the core. The daemon delegates legacy
`tb2_*` manifest validation to this application; the generic core needs no TB2
installation. A missing application is reported before daemon resource creation. Configuring `worker.tb2_tasks_dir` registers
`tb21-canonical-v1`; without that configuration the generic worker does not
load the application. A missing application fails before worker state is
allocated. The legacy `tb2_evaluate` request remains supported. The generic
scheduled SDK can also call `evaluate("tb21-canonical-v1", timeout_s=12100)`;
this evaluator accepts no verifier overrides and preserves the official
binary reward and durable evidence requirements. See the
[worker scoring contract](../../docs/architecture/AGENT_ENVIRONMENT_CONTRACT.md#worker-评分插件).

## Prepare a new image

```sh
dsec-tb21 prepare-image --suite /data/tb21-case --task openssl-selfsigned-cert \
  --image sha256:<local-task-image-id> --tools-image sha256:<local-tools-image-id> \
  --kernel /opt/dsec/vmlinux-erofs --agent-source ./guest_agent.c \
  --busybox /usr/bin/busybox --network --output /data/openssl-prepared
```

This path requires Linux x86_64, Docker, GCC with static libc, `mkfs.ext4`, a
static BusyBox with the mount/chroot/ip applets, and an EROFS-capable guest
kernel. The pinned tools image must supply `mkfs.erofs` with `--tar=f`, `--aufs`,
LZ4 and compressed-tar support. Both image arguments are exact local image IDs;
the task's declared Docker image must resolve locally to the provided task ID.
Commands do not pull images or call package installers.

The preparer verifies OCI diff IDs, preserves whiteouts and layer order, builds
a private bootstrap template and produces a validated catalog. It preserves a
disk reserve before conversion and saves failures in `prepare-result.json`.
The standalone build currently produces a **file-ext4 writable root**, with up
to 12 direct EROFS layers, leaving room for other VirtIO devices; larger inputs
are rejected before conversion. It does not automatically compact oversized images,
convert roots to OverlayBD, provision the external verifier tool disk, or
guarantee that a maximum-size device layout fits every guest configuration.
Use an already prepared, validated catalog for those conditions. This build
path is distinct from the OverlayBD prepared-state reuse acceptance.

## Agent and training entry

Applications use installed `TB2DSecEnvironment` and `DSecAgentEnvironment` via
`reset`, durable `step`, `evaluate`, and `stop`. API agents own model calls and
reply parsing. Miles uses the packaged adapter with these variables:

```sh
export DSEC_TB2_TASKS_DIR=/data/tb21-case/tasks
export DSEC_TB2_ENVIRONMENT_CATALOG=/data/tb21-catalog.json
export DSEC_ROLLOUT_WORKER_SOCKET=/srv/dsec/tb21/worker/worker.sock
```

Supply the task ID as trainer metadata. Model sampling, tokens, logprobs,
training and any GPU integration patches belong to the trainer; installing
this application starts no model or training process. Core installation alone
does not install this package or prepare TB2.1 resources.

## Compatibility and package boundary

The historical `dsec_adapters.tb2_*` imports and `tb2_verifier_artifact` resolve
to the implementations in `dsec_tb21_case`. Their task rules and diagnostic
cache metadata are shipped only by this optional package. The core wheel does
not include those JSON manifests. Install the application before using the old
imports; the error names the installation command if it is absent.

The generic Miles policy-session wrapper remains in `dsec_adapters.miles_session`.
The historical `dsec_adapters.openenv_agent_function` aliases that wrapper and
forwards explicit OpenEnv/TB APIs to this application. The normal DSec loop
still runs through the DSec worker and requires no OpenEnv service. Retained
Miles-derived code uses Apache-2.0 with its complete license text; the project's
own code remains MIT.
