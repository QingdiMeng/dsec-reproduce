<a id="dsec-控制面-ha--kind-演示设计"></a>

# DSec control-plane HA demo with kind

**English** · [简体中文](README.md)

On **one Linux host**, take an entire logical node offline. A user performs an operation on a real MicroVM, loses the original API reply and retrieves the same result through a surviving API. Acceptance checks request identity, writable instance count, quota and time to recover. Kubernetes supplies the experimental topology. The DSec Edge on the host runs the workload.

TASK-027 supplied the configuration and test design. TASK-028 verified H0–H3 on one physical host. TASK-029 traced H4 to inotify and repeated recovery with two leader positions. Three independent physical failure domains remain an open gate under TASK-004.

<a id="拓扑和入口"></a>

## Topology and endpoints

| kind node | Workload | Host SDK endpoint | Host etcd endpoint for the DSec Node |
| --- | --- | --- | --- |
| control-plane | Kubernetes control plane and its own etcd | — | — |
| worker a | api-a, etcd1, dedicated PVC | 127.0.0.1:25443 | 127.0.0.1:25379 |
| worker b | api-b, etcd2, dedicated PVC | 127.0.0.1:25444 | 127.0.0.1:25380 |
| worker c | api-c, etcd3, dedicated PVC | 127.0.0.1:25445 | 127.0.0.1:25381 |

Each workload uses `dsec.demo/domain` to select its worker. The API and etcd each have a NodePort with `externalTrafficPolicy: Local`, and kind maps a separate loopback port to each worker. This lets the test select the admitting, failing and recovering API replicas. Three etcd members find each other through dedicated headless Services. Each PVC returns with its original worker. Stop and restart the **same** worker container to preserve node identity and local volumes. All four kind nodes share one host kernel and power supply.

The DSec NodeGateway and Edge run on the host. A dedicated instance directory provides capacity for one MicroVM. API Pods connect to the host-reachable NodeGateway address using mTLS. The host SDK uses three HTTPS endpoints. For workload acceptance, verify the client's CA and project credentials and the observed Node identity. Count Kubernetes nodes, DSec Nodes and voting etcd members separately.

<a id="准备清单"></a>

## Prepare the manifests

From the repository root, point `OUT` at a new, empty private directory dedicated to this run. Set `KIND_IMAGE`, `ETCD_IMAGE` and `API_IMAGE` to SHA-256-pinned image references available on the experiment machine. Build the API image from the wheel under test and the repository cluster Dockerfile. It must include Python, DSec and `/opt/dsec-initialize.py`. For offline use, preload the images and verify the digest resolvable on each worker. Record kind, kubectl, Kubernetes, etcd, wheel and image versions.

```bash
DEMO=deploy/cluster/demos/kind-ha
python3 "$DEMO/render.py" kind > "$OUT/kind.json"
python3 "$DEMO/render.py" quorum --image "$ETCD_IMAGE" > "$OUT/quorum.json"
python3 "$DEMO/render.py" initialize --image "$API_IMAGE" > "$OUT/initialize-job.json"
python3 "$DEMO/render.py" api --image "$API_IMAGE" > "$OUT/apis.json"
```

Generate role assets from a fresh single-Node demo deployment using [deployment preparation tool](../../prepare.py). Use dedicated project, IAM prefix, CA, ports and Node root. Use `--host-address 127.0.0.1 --port-base 25379` for the host etcd endpoints. For `--node-address`, choose a **host address reachable by the Pods** that matches the Node service certificate SAN. Controller Pods use short names `etcd1`, `etcd2` and `etcd3` internally. The generator's default client API ports target Compose. Configure the demo client with the three loopback addresses in the table instead. See [cluster deployment guide](../../../../docs/guides/CLUSTER_DEPLOYMENT.md) for Node startup and admission.

| Demo input | Source and preparation |
| --- | --- |
| ConfigMap `role-config` | Copy `config/controller.json` and `config/initialize.json` to a dedicated demo configuration directory. In the controller coordination settings remove `token_file` and retain the authentication username and password file. In the initializer set `export_node_token=false`. After initialization the API and host Node use existing etcd password authentication and renewal. In the demo host's `node.json` coordination settings likewise remove `token_file` and retain authentication. |
| Secrets `etcd1-tls`, `etcd2-tls`, `etcd3-tls` | Take each member's certificate, key, CA, health-check and JWT files from `secrets/etcdN`. Check that each member certificate covers its own short name and 127.0.0.1 for host access. |
| Secrets `api-secrets`, `init-secrets` | Use `secrets/controllers` and `secrets/initialize`, respectively. Mount the root password only in the one-shot initialization Job. DSec checks credential ownership, mode and file type. The initContainer copies projected files to a memory volume as private regular files owned by runtime UID 1000. Keep the CA signing key outside the cluster. |
| ConfigMap `checkpoint-driver` | Mount the repository's [verify_cluster_node.py](../../../../tools/verify_cluster_node.py) under its original name. Use its existing `--child-config` entry point. Only the demo API processes install the pre-reply checkpoint. |
| Host Edge | Provide a dedicated sandbox directory, KVM, boot artifacts and an admission budget. The Node reads and writes the same prefix through the three etcd TLS endpoints above. Record the instance baseline, existing host services and leases. |

Create a kind cluster named `dsec-ha-demo` with its own kubeconfig, for example `kind create cluster --name dsec-ha-demo --image "$KIND_IMAGE" --config "$OUT/kind.json" --kubeconfig "$OUT/kubeconfig"`. Pass that kubeconfig and context `kind-dsec-ha-demo` to every kubectl command. Create the namespace of the same name, the role Secrets and both ConfigMaps first. Import with `kubectl create secret generic ... --from-file=<role-directory>` so credentials are never written to manifests, logs or Git.

Apply the files in **quorum → initialize → api** order. Before proceeding at each stage verify three-member peer TLS and common cluster ID, completion of the initializer Job with authentication enabled, then authenticated SDK health on all three APIs. Start and activate the dedicated Node per the deployment guide. Confirm each replica sees the same eligible Node. Startup evidence must identify each Pod UID, worker placement, etcd member IDs and leader, and the local endpoint behind each NodePort. TCP readiness controls routing. Score actual authenticated operations.

<a id="故障测试协议"></a>

## Fault test protocol

The existing [three-member check](../../../../tools/verify_cluster_control.py) covers IAM and etcd quorum. The [lost-response check](../../../../tools/verify_cluster_node.py) covers two API processes and one real Node. This test adds **simultaneous loss of the API and etcd on a single worker**. Run the scenarios sequentially, restoring all three members and APIs before each round.

| Scenario | Action | Pass criteria |
| --- | --- | --- |
| H0 healthy baseline | Discover the same Node through each endpoint. Create a MicroVM, write and read a random marker inside it, then stop it. | Retain real SDK and Edge receipts. IAM usage and physical leases return to baseline. All three etcd members are healthy. |
| H1 lost reply and follower domain loss | Pick a worker with a follower. Submit the creation only to its API. Stop the entire worker at the pre-reply checkpoint. Query the original request ID and attach through a surviving API. | The original request is visible with the same sandbox ID and owner. Exactly one Edge creation, physical instance and admission exist. Execute a command and read the marker. Release that instance's capacity, then create and stop another under a new ID while the domain is still down. |
| H2 lost reply and leader domain loss | After restoring health, identify the current leader. Repeat H1 on its API. Sample the remaining members' leader and SDK results throughout the outage. | The remaining members elect a new leader. The original request can be queried without duplicate execution, and a new one is admitted. Report election and workload recovery times, including errors during the election. |
| H3 recovery | Start the same kind worker and wait for its local etcd volume and API to become available. | All three original member IDs and the cluster ID match. All three APIs can query historical evidence. Released requests incur no new instance or charge. |
| H4 quorum loss (extension) | After H3, retain one known instance. Stop two demo workers, send a new create request with a unique ID to the remaining API, then restore at least one vote. | No success receipt is possible without quorum. After quorum returns, reconcile timeouts and unknowns by original ID and Edge journal. Preserve the existing instance and owner. Verify no duplicate instances or leaked quota after cleanup. |

**Inject at a deterministic checkpoint.** Generate a unique request ID and write `{"request_id":"…"}` to `/gate/gate.json` in the target API Pod. Submit creation asynchronously with an SDK client configured only for that endpoint. When `/gate/boundary.json` appears, read `stage=result_before_reply`, request ID, PID and Pod UID from that Pod, check them, and save the evidence on the host immediately. By this point Edge has produced its result. Execute `docker stop -t 0` only on a worker verified by the dedicated cluster labels, node role and Pod placement. Choose an ungated surviving API as the explicit query endpoint. The checkpoint times out after 45 seconds. If injection misses the window, ownership is uncertain or the leader changes before injection, record a miss, restore health and retry.

For an unknown SDK outcome, query `lookup_request(original_id)` and attach using the recovered sandbox ID. If testing a retry, reuse the original request ID, parameters and project. Compare the Edge journal, IAM receipt, guest marker and physical lease. Record counts before and after create and stop. In a separate lost-reply test for a command with a side effect, use one command request ID to append a unique marker inside an existing VM. After recovery the marker must appear exactly once. Report command deduplication separately from creation deduplication.

**Timing and deadlines.** Use one host monotonic clock for the last successful pre-fault request, injection, first observation of a new leader, first successful query of the original request, first successful new create and return to member health. Record the one-second polling resolution. A round that exceeds the demo's 120-second wait fails. Save the last SDK error, etcd status and Pod events. Time request recovery and new VM creation separately. Restore the domain with H3 after each H1/H2. Do not calculate percentiles from one round.

**Cleanup and evidence.** In finally, restart the worker stopped by this run, then stop and reclaim the sandbox through the original owner. Verify IAM, Edge physical leases and the host services baseline before deleting the dedicated kind cluster. Retain topology, versions and image digests, per-round timelines, request and resource counts, cleanup and overall verdict. Reconcile UNKNOWN by original ID. If a lease remains unresolved, retain the control plane and private diagnostics and report failure. Remove tokens, private keys and logs with authorization headers from any public example.

<a id="linux-实机结果2026-11-24-至-25-日"></a>

## Linux hardware results, 2026-11-24 to 25

Four kind nodes shared one Linux host. The business workload used a dedicated Edge, the public SDK and a Firecracker MicroVM. Stopping a worker container disconnected both its API and etcd member. A host monotonic clock measured from issuance of the stop command. One complete timed round was recorded for each role.

| Stopped domain | First observed agreement of two remaining votes | Original creation found | New creation returned | All three APIs can query original record after original worker start command returns |
| --- | ---: | ---: | ---: | ---: |
| follower b | 0.65 s | 0.79 s | 3.72 s | 20.99 s |
| leader a | 0.54 s (new leader c) | 1.67 s | 4.59 s | 23.23 s |

In H0, all three endpoints discovered the same Node. H1 and H2 each stopped the domain at the pre-reply checkpoint. The initial SDK call returned an unknown outcome. Lookup, attachment and retry with the original ID added exactly one Edge creation record, one physical lease and one unit of IAM concurrency. The guest wrote and read its marker. Stop returned both counters to zero. A second instance could be created while the failed domain was still offline. Each first attempt at the new creation returned `NoPlacement` once. After verifying no admission, retrying the same ID one second later succeeded. All original member IDs and the cluster ID matched after restoration. In the separate command test, repeating the same append after losing its reply left exactly one line in the file.

H4 stopped workers b and c. A new request returned `CoordinationUnavailable`. After two votes recovered, that request had neither an IAM admission nor an Edge creation record. The original instance still executed commands and was released through the SDK. The additional requirement for all three members to recover within 120 seconds failed. On worker c, kube-proxy reported `too many open files` at startup and etcd peer discovery through cluster DNS timed out. The host's inotify instance limit was 128. This run lacked syscall evidence to distinguish FD exhaustion from the inotify limit, so host settings were preserved. Independent physical-host failure remained TASK-004. H4 three-member recovery did not pass in this run. The next section records the subsequent diagnosis and retest.

First deployment exposed a missing temporary directory in the initializer's read-only root. The manifests now supply bounded memory-backed `/tmp` volumes for initialization and the API, preserving read-only roots. The API image was also built with Docker without BuildKit. The first H0 assertion directly compared an SDK signed route to the Edge local ID. It was corrected to decode the route and check owner and admission before rerunning. Cleanup removed this run's four kind nodes, node volumes and dedicated network. IDs, start times and restart counts of all five existing host containers matched the baseline.

The [Linux acceptance record](acceptance-20261124.json) records attempts, image digests, monotonic timelines and cleanup evidence.

References: [kind configuration](https://kind.sigs.k8s.io/docs/user/configuration/), [Kubernetes node selection](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/) and [etcd failure model](https://etcd.io/docs/v3.5/op-guide/failures/).

<a id="h4-宿主-inotify-诊断与复测"></a>

## H4 host inotify diagnosis and retest

On 2026-11-25 the two-domain restart fault was reproduced with the same kind version and four-node topology. Host settings were `fs.inotify.max_user_instances=128` and `fs.inotify.max_user_watches=65536`. kube-proxy startup syscalls were captured on worker b. The first two observations below came from its startup wrapper. The third came from strace on kube-proxy itself.

| Observation | Result |
| --- | --- |
| UID, open FDs at startup, soft/hard FD limits | UID 0, 3 FDs, both limits 1,048,576 |
| Open a regular file and create an inotify instance in the same launch environment | `open("/dev/null") = 3`. After close, `inotify_init1()` returned errno 24. |
| kube-proxy creating its listener | `inotify_init1(IN_NONBLOCK\|IN_CLOEXEC) = -1 EMFILE`, followed by `failed complete: too many open files` |

In the control at limit 128, etcd2 remained unreachable more than 120 seconds after the two workers restarted. The restored majority released the original VM, returning physical leases and project concurrency to zero. Failure occurred creating an inotify instance, before adding a watch. All four kind nodes share the host, where node UID 0 maps to host UID 0. Concurrent node startup exhausted that user's instance quota. Linux can return `EMFILE` for either a per-process FD limit or a user inotify instance limit. Record the failed syscall, the result of opening an ordinary file and the process FD limits together. [Linux inotify_init(2)](https://man7.org/linux/man-pages/man2/inotify_init.2.html)

For this fault, read the host inotify instance limit, temporarily raise it as needed and rerun H4 with stock kube-proxy. The retest used 512 on this machine. Select a value appropriate to the host's baseline and load. The command below writes only if the old limit is below 512. The setting lasts for this boot. [kind known issues](https://kind.sigs.k8s.io/docs/user/known-issues/#pod-errors-due-to-too-many-open-files)

```sh
sysctl fs.inotify.max_user_instances fs.inotify.max_user_watches
if [ "$(sysctl -n fs.inotify.max_user_instances)" -lt 512 ]; then
    sudo sysctl -w fs.inotify.max_user_instances=512
fi
```

After raising the limit from 128 to 512, each round recreated its dedicated cluster and verified the stock kube-proxy image and command line. The same H4 procedure stopped workers b and c. Recovery timing began when the first worker restart command was issued.

| Leader before fault | Original three member IDs and cluster ID restored | Three healthy endpoints, all three APIs return the same request, guest marker read |
| --- | ---: | ---: |
| a | 19.16 s | 21.10 s |
| c | 18.93 s | 22.06 s |

Both attempts to write without quorum returned `CoordinationUnavailable`. After recovery the rejected request had no admission or Edge creation entry. The original VM had the same PID as before the fault. Stopping it returned physical leases, IAM admissions and concurrency usage to zero. Each round retired its cluster and four node volumes and verified identity, start times and restart counts of all five pre-existing host containers. One round was run for each leader position. The [H4 rerun record](h4-20261125.json) contains the original control, recovery timelines and cleanup receipts.
