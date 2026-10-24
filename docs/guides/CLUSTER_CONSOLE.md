# Cluster management console (development preview)

The optional console uses the existing stateless cluster API and IAM. It does
not connect directly to etcd, SSH or the Edge socket, manage models, or create a
second sandbox registry. Grafana remains the resource monitoring interface.
Install/provision the [cluster](CLUSTER_DEPLOYMENT.md) first; the console does
not provision certificates, IAM, storage or Nodes.

## Enable and connect

Set the **controller** configuration's `server.console` to `true`:

```json
{"server":{"host":"127.0.0.1","port":8443,"cert_file":"/etc/dsec/api.crt","key_file":"/etc/dsec/api.key","console":true}}
```

Merge this setting into your existing controller configuration; this fragment
is not a complete deployment configuration. Start it with the existing entry:

```sh
python -m dsec.host.cluster controller --config /etc/dsec/controller.json
```

Open `https://<certificate-hostname>:8443/console/`. The browser must trust the
API CA; do not bypass certificate verification. Enable the setting on each
replica behind the same TLS ingress if you want the console itself to remain
available across controller failures. Keep the ingress and CA provisioning
outside the sandbox runtime. Browser requests stay on the console's origin;
no cross-origin API or cookie session is added. The Node gateway cannot enable
this UI. The option is off by default and assets are packaged with the core
wheel, without frontend build/runtime dependencies.

Enter a project ID and a valid IAM bearer credential. Credentials remain in
page memory, not URL parameters, browser storage or cookies. Reloading requires
re-authentication; exit clears credentials, results and selected sessions.
Copy request/sandbox/session IDs before leaving the page. The static shell is
public; all data and actions require API authorization.

## Available pages

| Page | Supported behavior |
| --- | --- |
| Overview | Project quota, usage/delegation and configured-node health |
| Nodes | Healthy, stale, unreachable and never-observed nodes; explicitly labeled last observation, backend/environment capabilities and remaining resources |
| Sandboxes | Project-bound allocation history, owner IDs, explicit Node status queries, microVM/container create and stop |
| Native operations | Select a microVM, open/attach/close a session, bounded shell execution and first 64 KiB file preview |
| Projects / IAM | Current project, authorized immediate children, child delegation, current policy changes, credential issuance/revocation |
| Requests / audit | Creation admission phases, quota ownership, explicit original-ID lookup and project audit metadata |

Creation arguments must describe an environment already available on a Node.
The JSON field uses existing API arguments (`environment_id` for microVM;
`spec` for a container). The explicit CPU/RAM/disk/network demand must satisfy
the Node's trusted minimums. Demand does not change the published environment's
VM sizing. The UI exposes rejection evidence; it does not bypass admission.
Credentials are caller-generated and saved before issuance. Issue/revoke
requires `iam.credentials` at root. Child delegation and policy edits remain
bounded by ancestor ceilings. A project needs `sandbox.read` to browse its UI;
policy/audit require `iam.policy`. Root credential audit metadata is visible
only to root readers also holding `iam.credentials`. Buttons are convenience guards; the backend
re-authorizes every call, including reads at their CAS return boundary.

## State and recovery semantics

`ADMITTED`, `DISPATCHED`, `BOUND` and `RELEASED` describe quota/dispatch ownership,
not guest runtime. The sandbox table deliberately starts with `未查询` runtime
state. A running count cannot be derived from BOUND allocations. `查询状态`
asks the owning Node. Failed status reads show UNKNOWN and label the previous observation separately;
successful status queries include their observation time.
The interface does not continuously poll. It updates observation age locally
and marks the displayed healthy view STALE when its freshness window expires.
Press refresh to acquire a new observation; browser freshness never authorizes
server placement.

A missing Node remains visible, with its last observed resources labeled as
historical. A failed/expired watcher view is never a placement candidate and
does not release quota. A restarted controller may show UNOBSERVED until it
has probed the Node; this is a rebuildable view, not durable health state.

Each mutation gets a single stable request ID. Double submissions are blocked
while a call is pending; transport failures and oversized/unknown responses
are never automatically resent. Creation queries use the original ID with an
empty sandbox ID. Execution/stop queries also need the owner sandbox ID. A
DISPATCHED request missing from the Edge journal is UNKNOWN, not failed.
After a confirmed non-admission, a new attempt needs a new request ID. If the
controller origin disappears, open another configured replica/ingress,
re-authenticate and query with the saved IDs. IAM writes do not yet have a
common per-request receipt: reconcile project configuration/audit or credential
identity instead of treating the generic sandbox query as an IAM recovery API.

## Authorized browse API

All operations use authenticated `POST /v1/call` cluster envelopes:

- `project.get`: current project budgets, caller permissions, grants only for policy administrators.
- `project.list`: readable immediate children; enter a child to browse deeper.
- `admission.list`: selected project's creation quota receipts without creation arguments/commands.
- `sandbox.list`: receipts with bound sandbox owner IDs, including released history; runtime is null.
- `audit.list`: selected-project change metadata, not credential hashes or raw changed keys.
- `node.list`: configured fleet and derived health freshness; no sandbox IDs or credentials.

List arguments are `limit` (1–100, default 50) and optional opaque `after` from
`next_after`. Cursors are scoped to project and operation and hide skipped
project names. Pages are separately authorized snapshots; they are not a
stable snapshot spanning future updates. A removed/changed cursor record
requires restarting pagination. Reads validate credential, ancestors and the
returned records at one CAS boundary. Prefix scans retain the existing 4,096
record safety bound; larger clusters need indexed/server-side pagination before
this preview can scale. Hitting that bound fails explicitly, never truncates
silently. This iteration does not alter etcd-token renewal, unknown-dispatch
operator recovery or physical multi-host HA limits.

## Acceptance boundary

Portable regressions cover project isolation and redaction, revocation at the
read-return boundary, bounded pagination, stale/missing fleet views, lost
creation replies queried through another controller without replay, release
history, read-only transport classification, optional asset hosting, exact
asset paths and security headers. Initial browser fault acceptance uses the real API/IAM/NodeGateway over an
explicitly simulated Edge. A separate Linux gate used two Mac controllers,
a dedicated three-member etcd and one real Firecracker VM: the UI created it,
opened a native session, executed a command, read its output file, reattached
the same session through the other controller, and stopped the VM with project
usage returning to zero. Pausing only the test Node gateway made it UNREACHABLE
while preserving the historical active count and quota; the guest continued
after the gateway resumed. This gate uses a private loopback HTTP browser
ingress and verified TLS/mTLS to the Node, not a production browser TLS ingress
or independent physical control-plane failure domains. Static assets ship with a strict CSP, no inline scripts,
no dynamic HTML injection, no CDN dependencies and no credential persistence.
Native streaming, large/binary uploads, container native sessions, fleet-wide
live runtime aggregates, quota resizing, credential renewal and OIDC login
remain separate work. Use the existing native SDK for file uploads and streams.
