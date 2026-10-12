# HA cluster deployment (development preview)

This deployment adds IAM, remote API, placement and watchers to existing Edges.
It does not provision guest images, Firecracker, KVM, network privileges, storage,
model servers or trainers. Install the core package first and provision each
Linux Edge using [host configuration](DSEC_HOST_CONFIGURATION.md). A macOS
machine can run controller processes; it cannot be a Firecracker Node.

## Docker preview with an external Linux Node

The source distribution includes [Compose](../../deploy/cluster/compose.yaml),
a [configuration generator](../../deploy/cluster/prepare.py),
an [initializer](../../deploy/cluster/initialize.py) and an
[installed-wheel image](../../deploy/cluster/Dockerfile). This topology runs two
API/controller replicas and three **real etcd members** on one Docker host.
The sandbox Edge and optional rollout worker remain on a separately provisioned
Linux machine. It tests process/member failures, not loss of the Docker host.
It does not install Docker, provision Nodes or start a trainer.

Build the core wheel from the source distribution, and supply immutable image
references available for your Docker architecture. The acceptance used
`python:3.12.15-slim` and `quay.io/coreos/etcd:v3.6.5`, resolved to repository
digests. The wheel is installed with `--no-index --no-deps`; Docker BuildKit may
still need registry access to resolve base-image metadata.
Run the generator as the dedicated non-root service account; Compose uses that
account's UID/GID to read its private credential files. Do not run preparation
through `sudo` or relax secret-file permissions to resolve ownership failures.

Prepare a Node spec using the actual socket paths and trusted resource minima:

```json
{
  "id": "node1",
  "edge_socket": "/srv/dsec/edge/service.sock",
  "worker_socket": "/srv/dsec/worker/worker.sock",
  "max_sandboxes": 2,
  "backends": ["microvm"],
  "environments": ["default"],
  "minimum_demands": {
    "default": {"concurrency": 1, "cpu": 1, "memory_mb": 512, "disk_mb": 1024, "network_mbps": 1}
  }
}
```

Omit `worker_socket` for native sandbox access only. Advertise MBPP/TB2.1
environments only after their separate application packages, artifacts and
verifier configuration are installed. Preserve those case settings when starting
the worker; for example, a prepared TB2.1 cache still requires the selected
`DSEC_TB2_VERIFIER_ONLINE` policy. Copying the gateway config does not configure
the worker or provide network access. Wait for Edge and worker health before
opening application jobs.

Choose free ports and explicit addresses reachable between Docker and the Node.
On macOS use the LAN address, rather than a Docker-internal hostname. Replace
the example addresses and all paths below. Set `DSEC_PYTHON_IMAGE` and
`DSEC_ETCD_IMAGE` to complete `repository@sha256:...` references, not tags.

```bash
python -m build --wheel --outdir dist/core .
export DSEC_PREVIEW_DIR="$HOME/dsec-compose-preview"
python deploy/cluster/prepare.py \
  --out "$DSEC_PREVIEW_DIR" \
  --wheel dist/core/dsec_reproduce-0.1.0.dev0-py3-none-any.whl \
  --python-image "$DSEC_PYTHON_IMAGE" --etcd-image "$DSEC_ETCD_IMAGE" \
  --project dsec-preview --host-address 192.0.2.10 --node-address 192.0.2.20 \
  --node-directory /srv/dsec/gateway-preview --node-spec /tmp/node-spec.json

docker pull "$DSEC_PYTHON_IMAGE"
docker pull "$DSEC_ETCD_IMAGE"
cd "$DSEC_PREVIEW_DIR"
docker compose build api1
docker compose up -d etcd1 etcd2 etcd3
docker compose run --rm initialize
```

The generator refuses an existing output directory. Keep that directory private:
it contains a preview CA, passwords, keys and credentials. `deployment.json`
records wheel/image/template identities; it contains no raw credentials. The
initializer enables etcd authentication and grants its service identity access
only to `/dsec/compose/<project>/`. Gateway client certificates have no Common
Name and use explicit etcd tokens; peer TLS and client certificate validation
remain enabled. Neither API replicas nor the Node receive the CA signing key
or root password. The containers use isolated Compose networking and volumes,
read-only roots and dropped capabilities; there is no privileged mode, host
networking or Docker socket mount. Initialization alone has writable credential
exports.

Transfer the contents of `node/` securely to the exact `--node-directory` on
Linux. Its secrets must be owned by the Node service user and mode `0600`.
Start the existing Edge and worker, then the installed gateway:

```bash
python -m dsec.host.cluster node --config /srv/dsec/gateway-preview/node.json
```

On the Docker host, start the API replicas using the initialized credentials:

```bash
docker compose up -d --no-deps api1 api2
docker compose ps
```

Give callers `client.json` and the referenced **API CA and IAM token only**;
adjust its file references after copying. Do not distribute the whole secret
directory. API endpoints default to ports 18440/18441, etcd to 18430–18432 and
the Node gateway to 18444. The management console is enabled at `/console/`.
No ingress/load-balancer container is included; read-only SDK calls can fail
over, while a lost mutation reply requires reconciliation by its original ID.

Preview certificates expire after 30 days and etcd JWTs after 24 hours.
Generated services automatically log in and renew tokens in memory using the
prefix-restricted `dsec` identity and its private password file. Concurrent
renewal is serialized in each process. A still-valid token can cover a temporary
renewal outage; expired or explicitly rejected tokens fail closed. Only an
explicit HTTP 401 / gRPC UNAUTHENTICATED rejection permits one authenticated
resend. Permission denial and uncertain transport replies do not use that path.
No root password or token-renewal restart is required by API/Node replicas.
Certificates, route keys and service-password rotation still require an operator
workflow. A configuration with only `token_file` retains static-token behavior.
Keep the output directory and data together. `docker compose down` retains data;
`down -v` discards named quorum volumes in the single-host preview. Cross-host
exports use private bind directories, which must also be retained.

Docker preview acceptance on 2026-10-12 passed two installed-wheel controllers,
TLS/authenticated three-member etcd and one physical Linux Node. Controlled
create/command reply loss recovered through the second API without duplicate
effects; one-member loss kept commands working, two-member loss rejected new
creation, and restoration retained the same VM and quota before clean release.
Fixed-response Miles and MBPP checks, including eight independent MBPP episodes
at concurrency two, passed. This is harness evidence, not GPU training or a
multi-node placement/physical-host HA result. Native sessions/streams require
a matching native guest; the legacy counter guest in this Docker acceptance
does not support them.
The separate TB2.1 `regex-log` fixed trajectory passed the original official
verifier with reward 1 and complete CTRF evidence after preserving its prepared
offline cache policy. Worker health transport failures and a readiness loss after placement now return
a typed UNKNOWN with the original admission retained. Explicit recovery uses the
saved episode and original Node. The formal readiness invariant and controlled
implementation regressions cover this boundary; `NOT_FOUND` alone never proves
a dispatched request safe to resend.

## Cross-host role exports

Install Docker Engine and Docker Compose v2 or later on each host. The generator
and exporter run on the administrator machine; no Docker socket enters service
containers. Provide `--topology /path/topology.json` to `prepare.py` using three
etcd members and two controllers. This example has three independent addresses:

```json
{
  "etcd": [
    {"address": "192.0.2.10", "client_port": 2379, "peer_port": 2380, "uid": 1000, "gid": 1000},
    {"address": "192.0.2.20", "client_port": 2379, "peer_port": 2380, "uid": 1000, "gid": 1000},
    {"address": "192.0.2.30", "client_port": 2379, "peer_port": 2380, "uid": 1000, "gid": 1000}
  ],
  "controllers": [
    {"address": "192.0.2.10", "port": 8443, "uid": 1000, "gid": 1000},
    {"address": "192.0.2.20", "port": 8443, "uid": 1000, "gid": 1000}
  ]
}
```

Use actual service-account IDs and free reachable ports. Services on the same
host must use the same account. Repeated addresses are allowed for a development
rehearsal, but reduce fault domains. Do not start the master Compose file for
this mode. Export host-specific directories:

```bash
python deploy/cluster/export_hosts.py --prepared "$DSEC_PREVIEW_DIR" \
  --out /private/dsec-host-exports
```

Securely transfer only each address's directory to that host. Transfer the
separate `node/` export to its configured Node directory, and `client.json` with
only its referenced API CA/IAM credential to callers. Preserve private modes;
on each host the deployment directory, credentials and `data/` must be owned by
its configured account. Do not copy the master `pki/` or root password to every
machine. Only the first controller host receives the initializer's credentials.
The exporter never starts services.

In each host directory, build its API image if it has one, then start its local
etcd service(s). Start all members before running `docker compose run --rm
initialize` on the first controller host. Wait for authenticated quorum health,
start the Node's Edge/worker/gateway, and start each host's own API service with
`docker compose up -d --no-deps api1` or `api2`. Remote dependencies are not
represented as local Compose services. Re-running preparation with new keys
against existing data is not credential rotation.

Peer servers retain address-matching certificates. A separate CA-signed,
client-only certificate identifies each peer; the exact three member CNs are
allowlisted. This accommodates source-IP rewriting by Docker NAT without
turning off server TLS verification or accepting other client identities. See
[etcd's peer TLS configuration](https://etcd.io/docs/v3.6/op-guide/configuration/).

Two-host acceptance on 2026-10-12 used an API replica on each host, two etcd
members on Mac, the third member on Linux, and the physical Linux sandbox Node.
Seven controlled gates passed: typed UNKNOWN at worker readiness loss,
original-episode recovery, cross-host API failover with the same VM and one
command effect, long-lived operation after JWT expiry, minority-member loss,
majority-member loss with coordination rejection, and rejoin/clean quota release.
These faults stopped only dedicated service roles; neither physical host nor its
Docker daemon was stopped. Separate real-auth tests used a 12-second JWT TTL
and proved proactive renewal, one refresh after a server-expired token rejection,
and prefix permission denial. The canonical deployment keeps a 24-hour TTL.
Dedicated peer certificates passed positive/negative TLS identity checks.
Eight installed-wheel harness checks passed again through the Linux API,
including the Miles loop, MBPP fixtures and eight episodes, and TB2.1's original
regex-log verifier with score 1. This campaign used no model or GPU training.

With two physical machines and three members, one machine necessarily holds a
majority. Its loss prevents quorum. Two-host tests can establish cross-host
routing and API/member failover, but cannot establish availability after any
single physical-host loss. The latter requires a third independent fault domain
and an available ingress, followed by a separate failure campaign.

## Prerequisites and trust

Use at least two controller replicas behind an available ingress and three
etcd v3 quorum members in independent failure domains. The controller accepts
TLS with bearer IAM credentials. Each Node requires controller mTLS and its own
internal bearer credential. etcd must enforce authenticated identities and
restrict this deployment's key prefix. Do not expose an unauthenticated etcd
gateway or the private Edge socket. The JSON gateway requires an etcd-issued
authentication token. Configure `coordination.authentication` for automatic
login/renewal, or `coordination.token_file` for a static token. TLS Common Name
alone does not authorize it, as described in the [etcd gateway documentation](https://etcd.io/docs/v3.6/dev-guide/api_grpc_gateway/).
With automatic login enabled, services renew using their limited identity.
If expired credentials cannot be renewed, coordination fails closed.
The Node service identity is trusted to
attest only its own Edge; all services accessing IAM storage are trusted in this
preview, rather than mutually distrustful tenants.

Provision TLS certificates with SANs matching the configured addresses,
`subjectKeyIdentifier`, `authorityKeyIdentifier`, appropriate key usage and a
trusted CA. Node server certificates require `serverAuth`; controller-to-Node
client certificates require `clientAuth`. Protect keys and credentials with
mode `0600`, owned by their service account. Keep CA private keys separate.
All replicas use the same private 256-bit route-integrity key, represented as
64 hexadecimal characters. All Node gateways use that key too. Rotation of
route keys and tokens requires an explicit operator workflow; automatic key
rotation is not implemented.

## Node setup

Start an existing Edge with its own root and `--node-budget` configuration.
Verify `health` advertises `node-admission-v1` before adding a gateway. Edge
budgets must reserve host pressure floors and accommodate guest/artifact costs.
IAM quotas do not replace physical admission.

Create a JSON Node configuration. Replace every example path and address with
operator-provisioned values. The gateway advertises only environments already
available on that Edge. Each advertised environment has a trusted minimum
demand; this prevents a client declaring zero resources to evade IAM quotas.

```json
{
  "coordination": {
    "endpoints": ["https://kv1.example:2379", "https://kv2.example:2379", "https://kv3.example:2379"],
    "prefix": "/dsec/production/v1/",
    "ca_file": "/etc/dsec/etcd-ca.crt",
    "authentication": {
      "username": "dsec", "password_file": "/etc/dsec/etcd-password",
      "renew_before_seconds": 300
    },
    "timeout": 3
  },
  "route_key_file": "/etc/dsec/route-key",
  "node": {
    "id": "node1", "edge_socket": "/var/lib/dsec/edge/service.sock",
    "gateway_root": "/var/lib/dsec/gateway", "max_sandboxes": 8,
    "worker_socket": "/var/lib/dsec/worker/worker.sock",
    "backends": ["microvm"], "environments": ["default"],
    "minimum_demands": {
      "default": {"concurrency": 1, "cpu": 1, "memory_mb": 512, "disk_mb": 1024, "network_mbps": 1}
    }
  },
  "server": {
    "host": "0.0.0.0", "port": 8444, "max_requests": 32,
    "cert_file": "/etc/dsec/node.crt", "key_file": "/etc/dsec/node.key",
    "client_ca_file": "/etc/dsec/controller-ca.crt",
    "node_token_file": "/etc/dsec/node-token"
  }
}
```

```bash
python -m dsec.host.cluster node --config /etc/dsec/node.json
```

Only one gateway may own a given Edge socket. Its process lock is tied to the
Edge directory, even if another configuration chooses a different gateway
root. The gateway has no new Docker/KVM/storage driver and makes no firewall
changes. Health inspection uses bounded read timeouts; receipt reconciliation
runs independently so a delayed journal query does not occupy a health probe.

## Controller setup

Each controller uses the same `coordination` section and `route_key_file` as
above. Add the following sections and start at least two independent instances,
with certificates matching their exposed endpoints:

```json
{
  "bootstrap_token_file": "/etc/dsec/bootstrap-token",
  "root_quota": {"concurrency": 16, "cpu": 16, "memory_mb": 8192, "disk_mb": 16384, "network_mbps": 16},
  "nodes": {
    "node1": {
      "endpoints": ["https://node1.example:8444"],
      "token_file": "/etc/dsec/node1-token",
      "tls": {
        "ca_file": "/etc/dsec/node-ca.crt",
        "cert_file": "/etc/dsec/controller-client.crt",
        "key_file": "/etc/dsec/controller-client.key"
      }
    }
  },
  "watcher_poll_seconds": 2, "watcher_max_age": 10,
  "placement_choices": 2,
  "server": {
    "host": "0.0.0.0", "port": 8443, "max_requests": 32,
    "cert_file": "/etc/dsec/api.crt", "key_file": "/etc/dsec/api.key"
  }
}
```

Provision a random bootstrap token in a private file. Initialize IAM once:

```bash
python -m dsec.host.cluster bootstrap --config /etc/dsec/controller.json
python -m dsec.host.cluster controller --config /etc/dsec/controller.json
```

Bootstrap is idempotent only with the same identity and root quota; it does not
silently change an existing quota. Delegation, policy and credential operations
are available through authenticated `/v1/call` JSON envelopes. They require the
appropriate `iam.*` permission. Child budgets consume the parent's quota, and
child permissions cannot exceed the delegator or ancestor policy ceiling.
Credential creation accepts a caller-generated `new_token` so an interrupted
response cannot strand a credential unknown to the caller. Raw credentials
are not stored in etcd; successful IAM changes record atomic audit entries.

For lifecycle calls, every envelope contains `project`, a 32-character lowercase
hex `request_id`, `operation`, `sandbox_id` (null for create) and object `args`.
Creation also requires an explicit `resource_demand`. The public `DSecClient`
builds these envelopes and only connects to verified HTTPS API endpoints. It
requires IAM credentials and a project; `DSEC_SOCKET` is never a fallback.
`ClusterClient` remains an alias for the same API implementation. Local tests
and node-internal workers explicitly import `DevEdgeClient` from
`dsec.sdk.development`; legacy `libdsec_compat.DSecClient` retains that behavior.

## Optional management console

Enable `server.console: true` on controller replicas to serve `/console/`.
See the [console guide](CLUSTER_CONSOLE.md) for IAM, browser trust, supported
operations and recovery limits. It is disabled by default; Node gateways cannot
serve it.

## Remote native SDK

For the rollout/task API used by Miles and verl, also configure the optional
Node `worker_socket`. It must be a durable scheduled worker using this same
Edge. The gateway advertises `rollout-api-v1` only after checking that binding
and health. Register each application environment with an appropriate trusted
minimum demand. MBPP requests 1 CPU, 512 MiB RAM, 256 MiB disk and no network;
use a matching minimum for `python-mbpp`, or increase its application demand.
TB2.1's environment and verifier are provisioned on the Node separately.

Give applications a client JSON file containing credential references:

```json
{
  "endpoints": ["https://api1.example:8443", "https://api2.example:8443"],
  "project": "root",
  "token_file": "/run/secrets/dsec-token",
  "ca_file": "/etc/dsec/api-ca.crt"
}
```

The token file must be mode `0600` and owned by the client user. Relative file
references resolve against the JSON file's directory. Provision these files on
trainer/reward workers; raw tokens never belong in Hydra overrides or receipts.

```python
from dsec.sdk import RolloutAPIClient

async with RolloutAPIClient.from_config("/run/dsec/api-client.json") as client:
    episode = await client.attach(saved_rollout_id)
    dialogue = await episode.dialogue()
```

The route is application → API Server → authenticated Node gateway → private
rollout worker → Edge → sandbox. The worker owns steps, dialogue, verifier
evidence and recovery. Creation reserves IAM quota and fixes the Node before
dispatch. Public rollout IDs are namespaced by project in the worker. After
dispatch, even `NOT_FOUND` cannot authorize another create send; attach/status
query the original owner through any API replica. Released admissions cannot
re-create an episode. A definitive `NoPlacement` before admission waits with
the same episode ID and a decreasing RPC/placement deadline; UNKNOWN and other
errors do not enter this retry path. Deadlines cover TB2.1's verifier budget; health uses a
separate short worker channel. UNKNOWN never becomes zero reward or permits
another Node. Quota release requires an authoritative physical cleanup result.
Worker-owned sandboxes require `rollout.*` operations to preserve bookkeeping.

For a retained admission, an operator or application can explicitly call:

```python
episode = await client.recover_create(saved_rollout_id)
```

Recovery reauthorizes the project and baseline access, then uses the saved
canonical create arguments on the pinned Node. `ADMITTED` may cross the original
first-dispatch gate after health recovers. `DISPATCHED`/`BOUND` only inspect the
original worker record and never resend creation. Released admissions cannot
restart. An indefinitely unknown dispatched create remains charged and needs
additional operator evidence. Queued rollout cancellation is not implemented by the current worker.
Verifier failure details remain in its durable record and can be read through
status; this API does not expose arbitrary host files. One cluster owns each
Edge/worker pair. These are our protocol extensions, not the paper's wire format.

Check the fixed-response harness before GPU training:

```sh
python tools/verify_rollout_api.py --api-config /run/dsec/api-client.json \
  --mbpp-environment python-mbpp --samples 8 --concurrency 2 \
  --out /data/evidence/api-use-case-unique
```

Install `apps/mbpp` for that option. Add `--tb21-tasks`, `--tb21-catalog` and
`--tb21-trajectory` together to check a staged TB2.1 task and its official verifier;
that option needs `apps/tb21`. The bootstrap must match that task's ordered
layer manifest and command/verifier budgets; do not reuse the MBPP smoke
bootstrap. Networkless verification requires a prepared complete cache and
explicit environment configuration; a mounted cache alone does not establish
that a task can run offline. Fixed responses validate the harness, not model
accuracy, gradients or another training-quality comparison.

```python
import asyncio
from pathlib import Path
from dsec.sdk import DSecClient
from dsec.contracts.resources import NodeDemand

async def main():
    async with DSecClient(
        ["https://api1.example:8443", "https://api2.example:8443"],
        token=Path("/run/secrets/dsec-token").read_text().strip(),
        project="root", ca_file="/etc/dsec/api-ca.crt",
    ) as client:
        sandbox = await client.run_microvm(resource_demand=NodeDemand(
            cpu=1, memory_mb=512, disk_mb=1024, network_mbps=1))
        try:
            session = await sandbox.open_session()
            await sandbox.write_file("/tmp/input", b"binary\x00data")
            result = await session.run_shell("wc -c /tmp/input")
            print(result)
            await session.close()
        finally:
            await sandbox.stop()

asyncio.run(main())
```

Persist sandbox, session and operation IDs in the caller's job state. A lost
mutation response raises `RequestOutcomeUnknown`; the SDK does not resend it to
another API. Reconnect and use `lookup_request(create_id)` for creation,
`lookup_request(command_id, sandbox_id=...)` or `session.query(command_id)` for
commands, and `session.events(stream_id, cursor=...)` for stream reattachment.
A read-only call may fail over between configured endpoints.

Before sending a create, IAM pins its Node and the Node durably marks
`DISPATCHED`. If the Edge journal is still `NOT_FOUND`, the outcome is unknown
and quota stays held: the send may still be queued. A confirmed Edge resource/admission rejection
before effects closes that admission; retry with a **new** operation ID after
examining current capacity. This preview does not silently reassign an existing
admission. An unreachable Node is removed from placement, while its existing
admissions remain charged. Never treat health loss as proof a VM stopped.

## Live episode service restart acceptance

`tools/verify_rollout_restart.py` checks a committed agent action across
operator-controlled service restarts. Use a dedicated project and Edge with no
other admitted jobs. The tool creates one counter episode, persists its dialogue,
reconnects by the same public ID, and explicitly resubmits the same committed
action identity to verify journal deduplication. It then executes another agent
step, runs the counter verifier, stops the episode and verifies cleanup.

Supply a private JSON plan with explicit command argv arrays. These are
operator-owned commands for this dedicated deployment; the tool does not invoke
a shell, install services, choose PIDs or change host Docker/Grafana. For example,
replace the following commands with reviewed executable scripts:

```json
{
  "inspect": ["ssh", "node1", "/srv/dsec/acceptance/inspect-vmm", "{rollout_id}"],
  "inspect_outage": ["ssh", "node1", "/srv/dsec/acceptance/inspect-quota", "{rollout_id}"],
  "phases": [
    {"name": "all_api", "stop": [["/srv/dsec/acceptance/stop-apis"]], "start": [["/srv/dsec/acceptance/start-apis"]]},
    {"name": "gateway_worker", "stop": [["/srv/dsec/acceptance/stop-gateway-worker"]], "start": [["/srv/dsec/acceptance/start-gateway-worker"]]},
    {"name": "api_node_edge", "stop": [["/srv/dsec/acceptance/stop-api-node-edge"]], "start": [["/srv/dsec/acceptance/start-api-node-edge"]]}
  ]
}
```

The read-only `inspect` command must produce one JSON object with `physical`
containing `sandbox_id`, positive integer `pid` and `/proc` `start_ticks`, and
`boot_id`; also include `held_concurrency`, `edge_leases` and `admission_state`.
Resolve the public episode's persisted worker record to its physical sandbox.
After stop, `physical` may be null. `inspect_outage` is optional and returns only
`held_concurrency` and `admission_state` from authenticated IAM storage. This
separate Node/operator inspection supplies physical evidence; application work
continues to use the API exclusively. Inspection must never execute guest work.
Stop/start commands must be idempotent, scoped to owned services and finish
within 45 seconds; starts must launch services without waiting for the caller
process to remain attached. Keep etcd running throughout these service tests.

```bash
python tools/verify_rollout_restart.py --api-config /run/dsec/api-client.json \
  --plan /private/restart-plan.json --out /data/evidence/service-restart-unique
```

The report preserves partial failures, source/plan identities and command logs.
It requires the VMM PID, process start time and boot ID to remain identical,
retained history/dialogue to match exactly, and quota/Edge leases to remain one
until authoritative cleanup returns them to zero. This gate covers a quiescent
live episode with an already committed action; it does not establish recovery
of a killed in-flight verifier, native stream, model session or physical host.

The 2026-10-12 two-host installed-wheel acceptance passed nine checks across
three restart combinations: both API replicas; gateway plus worker; and both
API replicas plus gateway, worker and Edge. Each combination retained the same
VMM PID, process start ticks and boot ID, the exact committed history/dialogue,
and one held admission/lease. Outages returned typed UNKNOWN without quota
release. The next agent step and counter verifier completed with reward 1;
cleanup returned quota and Edge leases to zero. The quorum and physical hosts
remained running. Preliminary tool failures were interface assumptions (dialogue
steps, legacy command timeout and the counter reward field), not runtime
recovery failures; their reports were retained and the corrected tool was rerun.
A protocol regression uses the real API and worker with fixture service controls
and execution to guard those assumptions separately from the physical evidence.

## Validation and remaining limits

Use `tools/verify_cluster_node.py` to launch two dedicated local controllers
against your provisioned Node and kill the accepting controller at controlled
create, command and stream response boundaries. It reclaims its one sandbox.
For example, with two free API listener ports in the supplied configurations:

```bash
python tools/verify_cluster_node.py --root /tmp/dsec-ha-acceptance \
  --config-a /etc/dsec/test-controller-a.json \
  --config-b /etc/dsec/test-controller-b.json \
  --token-file /etc/dsec/test-token --ca-file /etc/dsec/api-ca.crt
```

Use `tools/verify_cluster_control.py --help` for an isolated three-member etcd
fault test and `tools/check_cluster_refinement.py --help` for required code
mutations. The [design and acceptance record](../architecture/CLUSTER_CONTROL_DESIGN.md)
defines exact gates. A local quorum and two controller processes exercise
process/member failure; they do not establish availability after physical host
loss. Distribute ingress, controllers and quorum members before production.

This preview has no automated provisioning, external OIDC integration,
route-key rotation, quota resizing or operator recovery for an indefinitely
unknown dispatch. Baseline creation routing, the legacy TB2 container backend
and disk-IO project quotas are explicitly rejected. Native SDK operation is
generic; optional TB2.1 and RL adapters are separate applications.
