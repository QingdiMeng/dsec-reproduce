# HA cluster deployment (development preview)

This deployment adds IAM, remote API, placement and watchers to existing Edges.
It does not provision guest images, Firecracker, KVM, network privileges, storage,
model servers or trainers. Install the core package first and provision each
Linux Edge using [host configuration](DSEC_HOST_CONFIGURATION.md). A macOS
machine can run controller processes; it cannot be a Firecracker Node.

## Prerequisites and trust

Use at least two controller replicas behind an available ingress and three
etcd v3 quorum members in independent failure domains. The controller accepts
TLS with bearer IAM credentials. Each Node requires controller mTLS and its own
internal bearer credential. etcd must enforce authenticated identities and
restrict this deployment's key prefix. Do not expose an unauthenticated etcd
gateway or the private Edge socket. The JSON gateway requires an etcd-issued
authentication token (`coordination.token_file`); TLS Common Name alone does
not authorize it, as described in the [etcd gateway documentation](https://etcd.io/docs/v3.6/dev-guide/api_grpc_gateway/).
The current client fails closed when this token expires. Operators must renew
it and roll service instances; automatic etcd login/renewal is not implemented.
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
    "token_file": "/etc/dsec/etcd-auth-token",
    "timeout": 3
  },
  "route_key_file": "/etc/dsec/route-key",
  "node": {
    "id": "node1", "edge_socket": "/var/lib/dsec/edge/service.sock",
    "gateway_root": "/var/lib/dsec/gateway", "max_sandboxes": 8,
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
Creation also requires an explicit `resource_demand`. The Python client builds
these envelopes. Legacy local `DSecClient` remains usable over its private UDS.

## Remote native SDK

```python
import asyncio
from pathlib import Path
from dsec.sdk import ClusterClient
from dsec.contracts.resources import NodeDemand

async def main():
    async with ClusterClient(
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
