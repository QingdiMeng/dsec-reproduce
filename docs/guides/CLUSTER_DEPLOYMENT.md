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

Before generating that directory, run the same `prepare.py` command with
`--plan` appended to validate inputs and inspect the proposed controller/etcd
listeners, Node destination, wheel digest and physical etcd failure-domain count.
Planning reads the topology, Node specification and wheel only: it generates no
credentials, writes no files and needs neither OpenSSL, Docker nor an installed
DSec runtime. Run without `--plan` to generate the private deployment files;
generation remains separate from installation and service startup. The Python
`read_plan(args)` / `render(plan)` interface also rejects a changed wheel before
creating output. Plans are in-memory descriptions, not install receipts.

The single-Node arguments above remain compatible. For multiple external Nodes,
use the [batch inventory](#batch-node-inventory-and-shared-role-configuration)
below. Each microVM Node now has a read-only preflight and reviewed scoped
network-helper initialization workflow; see [Node setup](#node-setup).
Versioned Node registration, maintenance and retirement are available through
the management API as described below. Upgrade/rollback automation remains planned.
For CPU/GPU clusters, GPU training/inference services are external API clients;
GPU scheduling and sandbox GPU passthrough are not configured by this generator.

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

## Batch Node inventory and shared role configuration

Use `--nodes /path/nodes.json` instead of `--node-address`, `--node-directory`,
`--node-spec` and `--node-port`. They cannot be combined. Start from the shipped
[inventory example](../../deploy/cluster/nodes.example.json). Its format is:

```json
{
  "schema": 1,
  "root_quota": {"concurrency": 16, "cpu": 32, "memory_mb": 32768, "disk_mb": 65536, "network_mbps": 1000},
  "nodes": [
    {
      "address": "192.0.2.20", "port": 8444,
      "directory": "/srv/dsec/node1-gateway",
      "spec": {
        "id": "node1", "edge_socket": "/srv/dsec/node1-edge/service.sock",
        "max_sandboxes": 8, "backends": ["microvm"], "environments": ["default"],
        "minimum_demands": {
          "default": {"concurrency": 1, "cpu": 1, "memory_mb": 512, "disk_mb": 1024, "network_mbps": 1}
        }
      }
    }
  ]
}
```

Add one entry per Edge, with its actual IPv4 listener, private gateway directory
and already provisioned spec. Nodes can have different capacity, environments,
minimum demands and an optional `worker_socket`. GPU-only model servers are API
clients, not entries in this inventory unless they also run a sandbox Edge.
The schema has no GPU scheduling or model configuration fields.
`root_quota` is an optional IAM policy ceiling, not the sum of Node capacity.
Omitting it retains the preview defaults: concurrency 4, CPU 8, memory 8192 MiB,
disk 16384 MiB and network 1000 Mbps. The initializer and controllers receive the
same quota. Edge physical admission remains authoritative.

The planner rejects duplicate IDs, listener collisions with other Nodes or the
declared control/etcd roles, malformed resource fields and overlapping
gateway/Edge/worker roots between Nodes on the same address. Different addresses
may use the same paths. Validation is structural; it does not establish remote
host readiness, networking or available capacity.

```bash
python deploy/cluster/prepare.py \
  --out "$DSEC_PREVIEW_DIR" \
  --wheel dist/core/dsec_reproduce-0.1.0.dev0-py3-none-any.whl \
  --python-image "$DSEC_PYTHON_IMAGE" --etcd-image "$DSEC_ETCD_IMAGE" \
  --project dsec-cluster --host-address 192.0.2.10 \
  --nodes /path/nodes.json --plan
```

Append `--topology /path/topology.json` for the cross-host control/etcd layout
described below; without it, the control plane remains a single-Docker-host
preview. Run without `--plan` to generate a new private directory. One pure
`configuration.role_configurations` implementation builds the controller routes,
Node configurations, initializer, SDK configuration and Compose environment from
the validated plan. Both input modes use it; there is no separate batch runtime.

Batch output contains `nodes/<id>/node.json` and `nodes/<id>/secrets/` per Node;
the legacy input still produces `node/`. Every Node has a distinct server
certificate and internal token, and controller routing points to its matching
token file. Nodes receive no other Node's server key/token, controller private
key, bootstrap/root password or CA signing key. The existing trusted-node model
still shares the route key and prefix-limited etcd service identity; this is not
mutually distrustful Node/tenant isolation. Batch Nodes authenticate and renew
etcd credentials themselves, so no static Node token-export mount or subsequent
manual etcd-token copying is required.

Securely transfer the **contents** of each `nodes/<id>/` to its `directory` on
the corresponding Linux host, owned by its provisioned service account with
private modes preserved. Start its prepared Edge and optional worker first, then:

```bash
python -m dsec.host.cluster node --config /srv/dsec/node1-gateway/node.json
```

This completes initial configuration generation only. The generator refuses an
existing output directory and generates new identities: do not rerun it as an
in-place join or upgrade mechanism for an existing cluster. The scoped
microVM initialization commands below do not provision accounts, kernel modules,
packages or storage services. Complete automatic provisioning and
upgrade/rollback remain separate workflows requiring implementation and real-host
acceptance. Fleet enrollment changes membership through the management API;
it never regenerates an existing cluster's credentials.

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

Securely transfer only each address's directory to that host. For legacy input,
transfer the separate `node/` export to its configured Node directory. For batch
input, each host export contains only its local `nodes/<id>/` directories;
`host.json` maps these to their configured destinations. Move each Node's
contents to that destination under the Node service account. A Node-only host
gets no controller credentials, initializer or Compose service: its gateway is
the installed Linux process above. Transfer `client.json` with only its
referenced API CA/IAM credential to callers. Preserve private modes;
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

Newly generated configurations enable `node_lifecycle` on controllers and Nodes.
Nodes must be explicitly joined and activated after startup; a healthy Node alone
is not schedulable. Existing configurations without this setting retain their
legacy behavior. Upgrade all controllers, Node gateways and episode services
before enabling managed membership. Do not mix old admission code with this mode.
Finish pre-upgrade admissions before enrolling an old Node; unresolved work must
retain its original identity and quota. This is an explicit migration, not an
automatic reset of fleet state.

Before starting a microVM Node, follow the
[Host preflight and scoped initialization sequence](DSEC_HOST_CONFIGURATION.md#commands-and-acceptance)
with its generated `node.json` and prepared Host configuration. This detects
missing KVM/TUN access and required network capabilities in restricted containers
before preparing administrator material. The gateway also repeats the platform
guard before creating its state/lock or connecting to coordination when it
advertises `microvm`. Controller/bootstrap roles do not require KVM. A successful
preflight does not replace the execution and isolation acceptance below.

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

## Dynamic membership, maintenance and retirement

Fleet mutations require `iam.policy` on the **root project**. A tenant's policy
permission does not grant infrastructure administration. The operator client
below uses the generated private `client.json`; it never runs SSH, sudo or a
direct Edge operation. Every mutation saves its public intent and request ID to
a new `0600` result file before contacting an API Server. Keep these files when
a reply is lost. There is no automatic mutation replay or choice of another owner.

For an initial Node, use its generated `registration.json`. With the prepared
Edge/optional episode service and Node gateway already started:

```sh
python -m dsec.host.fleet --client /private/deployment/client.json --out join.json \
  join --spec /private/deployment/node/registration.json
python -m dsec.host.fleet --client /private/deployment/client.json --out activate.json \
  activate --node node1 --version 1
```

Read actual versions from the responses; the numbers here describe a fresh Node.
`JOINING` does not accept work. Activation requires an authenticated proof from
that Node, including usable Edge health. New quota admissions check ACTIVE in
the same etcd transaction as project usage and the Node's in-flight count; stale
Watcher observations cannot bypass a concurrent drain.

To add a completely new Node without updating or restarting controllers, create
one JSON entry in the batch inventory format (`address`, `port`, `directory`,
`spec`), with a fresh identity and disjoint host paths. On the administrator
machine holding the **original private master deployment and CA key**:

```sh
python deploy/cluster/enroll_node.py --deployment /private/deployment \
  --node /private/new-node-entry.json --out /private/new-node-bundle
```

Transfer that bundle privately to its declared Linux directory. Install the
same validated core wheel, complete Host preflight/scoped initialization, and
start Edge, the optional episode service and `node.json` gateway. Use the bundle's
`registration.json` in the join/activate commands. The issuing tool reuses the
existing CA and limited coordination credentials. It exports no CA private key,
bootstrap token or controller identity, and changes no controller configuration.
The controllers discover registered routes through etcd with bounded parallel
probes. The new gateway uses `node_token_mode: derived-v1`; verified TLS, controller
mTLS and a per-Node HMAC credential remain required. etcd stores public endpoints,
not these credentials. Nodes remain trusted cluster infrastructure, as in the
existing shared coordination/route-key design. This is not tenant self-enrollment.
Issuing a bundle does not register a Node or increase project quota.

The state sequence is `JOINING → ACTIVE → DRAINING → MAINTENANCE → ACTIVE`, or
`DRAINING/MAINTENANCE → RETIRED`. Start maintenance through the API:

```sh
python -m dsec.host.fleet --client /private/deployment/client.json --out drain.json \
  drain --node node1 --version 2
python -m dsec.host.fleet --client /private/deployment/client.json get --node node1
# Wait for admitted work to finish, or explicitly stop it through its normal API.
python -m dsec.host.fleet --client /private/deployment/client.json --out maintain.json \
  maintain --node node1 --version 3
# Apply the separately reviewed host maintenance while no owned resources remain.
python -m dsec.host.fleet --client /private/deployment/client.json --out resume.json \
  resume --node node1 --version 4
```

Drain fences **new admissions**, not already accepted work. Existing sandboxes and
episodes keep their owner and can continue/stop. Finishing maintenance requires
both zero IAM in-flight admissions and an owning Node proof that Edge leases,
sandboxes and episode/scheduler work are empty. Ready-pool or private development
resources can also prevent this proof: disable/refund prepared work using the
existing instance operations before finishing maintenance. These commands do not
kill active jobs, flush a ready pool, install privileges or upgrade software.
An unreachable Node or UNKNOWN work stays blocked and retains quota.

For exit, drain first, then call `retire --node node1 --version VERSION` after
cleanup. Retiring requires the same proofs; stopping the Node process or removing
its configuration is insufficient. Stop its now-idle services and explicitly
retire its administrator installation afterwards. This API retires cluster
membership, not Linux files or helper permissions. The Node identity stays as a
tombstone; physical endpoints may be reused only by a new identity. Existing
receipts and sandbox routes keep their original owner. To abandon a registration
that never became ACTIVE, `withdraw` converts JOINING to a tombstone using the
quorum proof that no controlled work was admitted; it does not claim host cleanup.

Every change requires an expected version. Repeat a lost-reply transition only
with its retained request ID, original arguments and version, using a new result
file. A completed latest transition reconciles; a delayed request after a later
transition fails rather than reactivating a retired Node. The model
`NodeLifecycle.tla` and controlled CAS tests cover this boundary. Mixed old/new
admission code, empty-host provisioning and certificate renewal
require their own migration/acceptance; membership does not silently supply them.

## Compatible runtime upgrade and rollback

This workflow upgrades the core Python runtime, with the original Node identity,
CA, credentials, paths and journals. It supports only equal explicit cluster,
fleet, coordination, Edge and episode compatibility contracts. The wheel version
and content build ID identify software; they are not the membership version used
for CAS. A publisher must review actual compatibility before retaining a contract
version. This is not a state migration, etcd upgrade, VMM/kernel/helper update,
guest native-agent or model upgrade. Those need separate plans. Keep all replicas on releases that
understand pending upgrades. Deployments without release identity must first
install a release-aware baseline during a controlled maintenance window; they
cannot be silently adopted or used as an automatic rollback target.

On a Node's unprivileged service account, stage the publisher's verified core
wheel alongside the old installation. Obtain SHA256 from the trusted release
channel, rather than treating a hash calculated from an unknown download as trust:

```sh
python -m dsec.host.releases stage --wheel /private/dsec_reproduce-VERSION-py3-none-any.whl \
  --sha256 PUBLISHED_SHA256 --root /srv/dsec/releases
python -m dsec.host.releases verify --release-dir /srv/dsec/releases/CONTENT_BUILD_ID \
  > /private/candidate-release.json
```

Staging uses a separate venv, `--no-index --no-deps`, and checks the installed
payload against the wheel. A completed identical stage can be verified/reused;
an interrupted directory without `release.json` is incomplete and is never
launched or silently reused. Retain/inspect that failed directory separately.
The old installation is untouched. Keep the old release and its verified build
reference until the observation window ends. Transfer only the public candidate
reference to the administrator's API client machine.

Drain and maintain the Node as above. Read the actual membership version, then
prepare the upgrade through the root-authorized management API:

```sh
python -m dsec.host.fleet --client /private/deployment/client.json --out upgrade.json \
  upgrade --node node1 --version MAINTENANCE_VERSION --release /private/candidate-release.json
```

The transaction requires zero held admissions, empty owning services, compatible
software and the running source release. It records source/target references in
the existing Node record. While pending, resume, retirement and another upgrade
are rejected. A lost reply retains the original request ID and version; inspect
`node.get` and reconcile that same request instead of inventing a second plan.

Stop this idle Node's **gateway, episode service, then Edge** through its existing
process manager. Update only its explicit service launch commands, preserving
Host/Node configuration and state roots. Each command runs in its own service:

```sh
python -m dsec.host.releases run --release-dir /srv/dsec/releases/CONTENT_BUILD_ID \
  --role sandbox --config /private/host.json --node-config /private/node.json
python -m dsec.host.releases run --release-dir /srv/dsec/releases/CONTENT_BUILD_ID \
  --role worker --config /private/host.json --node-config /private/node.json
python -m dsec.host.releases run --release-dir /srv/dsec/releases/CONTENT_BUILD_ID \
  --role node --config /private/node.json --node-config /private/node.json
```

Start Edge first, wait for its health, then the configured episode service and
its health, then gateway. Omit worker only if that Node has no configured episode
service. The launcher validates the loaded payload, membership and Host/socket
binding, then uses existing Host composition. Container agents follow the selected
installed package. It does not install privileges or control other processes.

After startup, use a new result file and the pending membership version to
`commit --node node1 --version PENDING_VERSION`. Commit queries the real gateway,
Edge and configured episode service; all must prove the target release and empty
resources. A startup failure, mixed versions, unavailable Node or UNKNOWN reply
does not authorize scheduling. After commit, explicitly `resume` with its new
version. Startup and Node admission also check the committed build; launching a
stale installation is not permission to serve new work.

To abandon a pending candidate, stop only its idle processes and launch the
retained **source** release with the same configurations, in the same order.
Then `rollback --node node1 --version PENDING_VERSION`, retaining its result file.
Rollback requires actual source software proof; it cannot succeed merely because
files were switched or the candidate died. It leaves MAINTENANCE until explicit
resume. Do not restore old etcd/Edge/episode state over current journals. To return
to an earlier version after a successful commit, prepare a new upgrade targeting
that retained compatible release; a late operation from the previous plan must
fail its version check.

For Docker control-plane replicas, create a separate credential-free image context:

```sh
python deploy/cluster/upgrade.py context --deployment /private/deployment \
  --wheel /private/dsec_reproduce-VERSION-py3-none-any.whl --sha256 PUBLISHED_SHA256 \
  --out /private/candidate-image
docker build --build-arg PYTHON_IMAGE=python@sha256:PINNED_BASE_IMAGE_DIGEST \
  --tag dsec-api-candidate:VERSION /private/candidate-image
# Use the exact pinned base from candidate-image/build.json.
# source-release.json is the release field of authenticated source API health.
# Record immutable local image IDs for both the old and candidate images.
python deploy/cluster/upgrade.py plan --deployment /private/deployment \
  --source-release /private/source-release.json --target-release /private/candidate-image/release.json \
  --source-image sha256:OLD_IMAGE_ID --target-image sha256:NEW_IMAGE_ID --out /private/api-upgrade
docker compose --project-directory /private/deployment -f /private/deployment/compose.yaml \
  -f /private/api-upgrade/upgrade-api1.json up -d --no-deps --no-build api1
```

Before changing api2, wait for **authenticated** health on api1 to report the exact
target release, query its IAM/Node state, and verify work still reaches its owning
Node. Read-only readiness polls may repeat within a bounded timeout; protected
mutations must not be replayed. Apply `upgrade-api2.json` only after these gates.
If a gate fails, retain the healthy replica and inspect the failed one; explicit
rollback uses `rollback-api1.json`/`rollback-api2.json` in the same one-at-a-time
sequence. Overlays pin only that API's image with `pull_policy: never`; they do
not replace configuration, credentials, etcd members or volumes. The tool renders
plans only and never calls Docker. Two colocated API replicas still do not prove
host-failure availability.

## Certificate and credential rotation

Rotation is explicit operator work. The SDK, controllers and Node gateways
reload selected security files per request/connection. Once enabled, rotations
below do not require restarting these processes or recreating sandboxes. An
accepted operation may finish after a retirement; requests accepted afterwards
must use current trust and credentials. This is not a global distribution or
acknowledgement service: verify each replica and Node before retiring material.
Keep private manifests, passwords and keys owned by the service user with mode
`0600`, and their directories `0700`. Never log or paste the secret values.

For an existing generated, managed deployment, first prepare role patches:

```bash
python deploy/cluster/rotate.py enable --deployment /private/deployment \
  --out /private/rotation-enablement
```

This writes patches only; it does not install files or restart services. Preserve
file modes when copying. Install each Node's secret manifests and patched
`node.json`, restart only its gateway, then install the controller manifests and
patched configuration and roll one API replica at a time. Keep Edge and episode
services running. This one-time opt-in changes configuration and needs the
supporting runtime on every participant. During migration, `allow_legacy: true`
accepts the previous per-Node credential. Do not disable it until every sender
uses the independent Node authentication ring. Route signing uses the unchanged
`route-key`; existing sandbox and episode IDs remain valid.

Each manifest contains a positive `generation`. Referenced material files have
absolute paths in the consuming service's filesystem namespace. Write new keys,
certificates and trust bundles to new private files, then publish their references
with the next generation and explicit expected generation:

```bash
python -m dsec.host.credentials publish \
  --kind tls-client --path /run/dsec/client-tls.json \
  --document /private/next-client-tls.json --expected-generation 1
```

Kinds are `tls-client`, `tls-server`, `node-auth` and `etcd-identity`. Publication
validates material, locks the destination, checks its expected generation and
atomically replaces the manifest. Publishing exactly the already selected
value is idempotent. A running reader rejects older generations and edits to
material or manifest at the same generation. Across restarts, the private
published manifest is the authority; never restore an earlier file to undo a
rotation. A deliberate rollback during overlap needs a **new** generation.

For read-only Docker role mounts, publish from the host with an explicit mapping:

```bash
python -m dsec.host.credentials publish --kind tls-server \
  --path /private/deployment/secrets/controllers/server-tls.json \
  --document /private/next-server-tls.json --expected-generation 1 \
  --reference-prefix /secrets \
  --material-directory /private/deployment/secrets/controllers
```

The publisher validates direct-child material against the private host directory
while preserving `/secrets/...` references in the published document. It rejects
path escapes and symlinks and grants no write access to the service. All files
must be installed before publishing the reference. The mapping also supports
`etcd-identity`; Node-auth documents need no path mapping. This avoids depending
on Docker Desktop's different bind-root UID mapping. Never relax ownership or
file/directory checks to work around a mount namespace.

### API access tokens

Only root IAM credential administrators can use this protocol. Prepare writes
the new private token, replacement client configuration and intent **before**
contacting the API. Preserve that output even if the response is lost:

```bash
python -m dsec.host.credentials prepare-api --client /private/client.json \
  --out /private/api-token-r2
python -m dsec.host.credentials check-api \
  --client /private/api-token-r2/client.json
python -m dsec.host.credentials get-api --client /private/api-token-r2/client.json \
  --rotation-id ROTATION_ID
python -m dsec.host.credentials complete-api \
  --client /private/api-token-r2/client.json --rotation-id ROTATION_ID \
  --out /private/api-token-r2/completion.json
```

Use the `rotation_id` from `intent.json`; do not substitute a new identity on
an uncertain result. Prepare permits both credentials. Complete requires the
**new** credential and revokes the old one in the same IAM transaction. Cancel
requires the **old** credential and revokes the candidate. Use `cancel-api` with
the same arguments and a fresh output path if abandoning an overlap. Query state
after response loss; the CLI never retries an uncertain mutation automatically.
A delayed cancel cannot restore a completed credential. Raw tokens never enter
the IAM ledger or audit output.

Distribute the selected replacement client/token privately. A client configured
with `token_file` reads its selected file each call, so an atomic replacement can
switch an existing SDK object. Do not rerun bootstrap with a revoked token or
reset the IAM ledger to make old credentials work. Keep a separate authorized
administrator available for recovery from operator mistakes.

### Node authentication and TLS

Node authentication uses a separate manifest of this form (values below are
placeholders; generate independent random 256-bit keys):

```json
{"generation":2,"active":"old","keys":{"old":"64_HEX_DIGITS","new":"64_HEX_DIGITS"},"allow_legacy":false}
```

Keep at most four keys. Publish the overlap on all receiving Nodes first, then
on controllers; switch controller `active` to `new` in a later generation. Check
every route before publishing a generation that removes `old`. No fallback to a
previous key occurs on errors. Long-lived route signing is a separate mechanism
and is not rotated by this operation.

Issue new leaves explicitly:

```bash
python deploy/cluster/rotate.py issue --ca CA.crt --ca-key CA.key \
  --cn NAME --host HOST --usage serverAuth --out PRIVATE_NEW_DIRECTORY
```

Use `clientAuth` for controller/etcd clients and repeat `--host` for each server
SAN. The optional `create-ca --cn NAME --out PRIVATE_DIRECTORY` creates a private
issuer; its key stays with the administrator, never on Nodes or API replicas.
For a DSec CA change, expand both peers' trust first, switch client and server
leaves, verify every peer, then remove the old CA. API client and controller-to-
Node manifests use `ca_file`, optional `cert_file`/`key_file`, and optional
`peer_sha256` leaf pins. Server manifests use `cert_file`, `key_file`, optional
`client_ca_file`, and optional client leaf pins. Set explicit fingerprint pins
when old leaves under a retained CA must be revoked. A renewed leaf alone does
not revoke another valid certificate issued by the same CA.

`python -m dsec.host.credentials check-api --client CLIENT.json` reports the
selected server TLS generation and public fingerprint. `python -m dsec.host.fleet
--client CLIENT.json security --node NODE` queries the Node through the API.
Check each API endpoint separately and compare expected leaf fingerprints;
otherwise a healthy first replica may hide an unrotated one. A connection that
completed its handshake before a trust/pin retirement is rechecked before an
application request is accepted. Requests are never transparently resent with
an older identity.

After opting in, new Node enrollment requires explicit `--ca CURRENT_CA.crt
--ca-key CURRENT_CA.key` arguments. It exports the currently selected limited
account, TLS and Node authentication manifests rather than old bootstrap
credentials. The enrollment tool does not infer or export an issuer private key.

### etcd service identity

Keep etcd administrator authentication external to service exports. Prepare a
fresh prefix-limited account using an administrator `EtcdStore` options JSON:

```bash
python -m dsec.host.etcd_rotation --admin-config /private/etcd-admin.json \
  prepare --username dsec-r2 --out /private/etcd-account-r2
```

The tool verifies the existing role grants exactly read/write access to this
deployment's isolated prefix. It records the new password and intent before
account creation; unknown administrative mutation replies are not retried.
Inspect account/role state and retained intent before resolving uncertainty.
Distribute only the new limited password to controllers and Nodes. Publish
`etcd-identity` manifests containing `generation`, `username` and the absolute
`password_file`; these replace the selected identity and discard any cached old
JWT immediately. Never try the previous account if new login fails. Prove access
through every service and to the existing ledger with the replacement account,
then retire the old one:

```bash
python -m dsec.host.etcd_rotation --admin-config /private/etcd-admin.json \
  retire --username dsec --replacement-config /private/etcd-new-service.json \
  --out /private/etcd-account-r2/retirement.json
```

The replacement config uses the same prefix and only the limited account. Retire
refuses `root`, checks the replacement's role and authenticated ledger access,
then deletes the old service user. An exact stale-auth-revision rejection
(HTTP 400/code 3) triggers one login with the selected identity and one resend;
etcd returns it before authorized effects. Generic validation errors, permission
denials and unknown replies do not enter that path
([etcd error definitions](https://github.com/etcd-io/etcd/blob/v3.5.21/api/v3rpc/rpctypes/error.go),
[authorization before apply](https://github.com/etcd-io/etcd/blob/v3.5.21/server/etcdserver/apply_auth.go)).
etcd authenticates and authorizes token
requests using its own auth state; a cached token is not an independent grant
([etcd auth design](https://etcd.io/docs/v3.5/learning/design-auth-v3/)). Renewing
an etcd client certificate through its TLS manifest does not replace its service
password or revoke old certificates. etcd peer/server CA changes and JWT signing
root changes require a separate member-by-member campaign, following
[etcd transport security](https://etcd.io/docs/v3.6/op-guide/security/).

The 2026-10-24 installed-wheel acceptance passed 21 checks with two API
replicas, three real etcd members on a Mac and one Linux Node. The same container,
marker and SDK object survived Node key, IAM token, API/Node CA and leaf,
etcd client leaf and limited-account rotation. Retired identities were rejected;
all five control-plane and three Node processes retained their original start
times. IAM held work returned to zero and Node retirement required empty proof.
The original five Linux host containers retained their process identities.
This proves this bounded topology and path, not independent-host HA or automatic
fleet-wide rollout. The first campaign exposed stale etcd auth-revision handling;
the exact rejection was fixed and the complete campaign rerun on the new wheel.

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
