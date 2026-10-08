"""Local Unix-socket JSON service, restricted to the server user's account."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import socketserver
import subprocess
import threading
from dsec.runtime.lifecycle import SandboxError
from dsec.storage.digest import sha
from dsec.runtime.isolation.network import NetnsNetworkManager
from dsec.runtime.requests import RequestJournal, MUTATING
from dsec.runtime.resource_rpc import sandbox_resource_sample
from dsec.runtime.admission_guard import AdmissionDenied, check_create

from dsec.control.server import BoundedServer, Handler, ServiceBusy

def main():
    from dsec.runtime.container_edge import ContainerRuntime
    parser=argparse.ArgumentParser()
    for flag in ("root","binary","kernel","template"):
        parser.add_argument("--"+flag,required=True)
    parser.add_argument("--socket-mode", choices=("0600", "0660"), default="0600",
                        help="Unix control socket permissions; 0660 is for a trusted local service drain")
    parser.add_argument("--node-budget", help="Edge-owned node budget JSON; incompatible with legacy worker admission")
    parser.add_argument("--admission-worker-socket", default=os.environ.get("DSEC_ADMISSION_WORKER_SOCKET"),
                        help="require a matching worker scheduler lease before microVM create")
    parser.add_argument("--egress-proxy-url",
                        help="HTTP(S) proxy injected into every guest command, e.g. http://192.168.0.106:18888")
    parser.add_argument("--egress-proxy-bypass-host", action="append", default=[],
                        help="Hostname accessed directly using no_proxy; repeat for each software source")
    parser.add_argument("--e3-artifacts")
    parser.add_argument("--e3-mixed-artifacts")
    parser.add_argument("--e3-binary")
    parser.add_argument("--tb2-template", action="append", default=[],
                        help="task-id=absolute ext4 path for a task microVM")
    parser.add_argument("--microvm-environment-catalog",
                        help="pinned generic microVM boot and EROFS layer catalog")
    parser.add_argument("--validate-only", action="store_true",
                        help="validate immutable artifacts and configuration without starting a daemon")
    parser.add_argument("--tb2-layer-manifest", action="append", default=[],
                        help="task-id=absolute EROFS layer manifest path")
    parser.add_argument("--tb2-overlaybd-root", action="append", default=[],
                        help="task-id=OverlayBD source image.json for opt-in ublk root disk")
    parser.add_argument("--overlaybd-ublk-socket")
    parser.add_argument("--overlaybd-global-config")
    parser.add_argument("--overlaybd-permission-container",
                        help="optional isolated probe container to set device group; host daemon uses a udev kvm rule")
    parser.add_argument("--tb2-manifest",
                        help="pinned TB2 suite manifest with per-task CPU and memory requirements")
    parser.add_argument("--tb2-free-page-reporting", action="store_true",
                        help="enable a zero-sized balloon with guest free-page reporting")
    parser.add_argument("--tb2-verifier-artifact-manifest",
                        help="pinned read-only TB2 verifier tool disk manifest")
    parser.add_argument("--tb2-verifier-artifact-local",
                        help="local path to the pinned TB2 verifier tool disk")
    parser.add_argument("--tb2-verifier-artifact-threefs",
                        help="optional 3FS path to the identical tool disk")
    parser.add_argument("--tb2-task-verifier-artifact", action="append", default=[],
                        help="task-id=manifest-path=local-ext4-path override")
    parser.add_argument("--tb2-verifier-dax-task", action="append", default=[],
                        help="task-id using a compact pinned verifier image via read-only pmem DAX")
    parser.add_argument("--tb2-verifier-dax-binary",
                        help="pinned Firecracker build used only for DAX verifier tasks")
    parser.add_argument("--tb2-network-tap",
                        help="dedicated host TAP for one networked TB2 microVM")
    parser.add_argument("--tb2-network-pool",
                        help="JSON file containing independent TB2 TAP/address slots")
    parser.add_argument("--tb2-netns-helper",
                        help="root-owned sudo-scoped helper for per-VM network namespaces")
    parser.add_argument("--tb2-netns-max-slots",type=int,default=8)
    parser.add_argument("--tb2-netns-dns",default="192.168.0.1")
    parser.add_argument("--tb2-netns-dax-binary",
                        help="explicit DAX Firecracker path pinned under the helper's dax39 alias")
    parser.add_argument("--capacity",type=int,default=4)
    parser.add_argument("--max-requests",type=int,default=8)
    parser.add_argument("--snapshot-concurrency",type=int,
                        help="maximum simultaneous Firecracker snapshot writes (default: capacity)")
    parser.add_argument("--snapshot-strategy",choices=("full","boot-diff","incremental"),default="full",
                        help="TB2 memory snapshot strategy; incremental rebases every later diff")
    parser.add_argument("--snapshot-cache-policy",choices=("retain","evict"),default="retain")
    parser.add_argument("--snapshot-editor",help="Firecracker snapshot-editor executable for incremental snapshots")
    parser.add_argument("--warm-pool", action="append", default=[],
                        help="task-id:verifier-storage:target ready VMs")
    parser.add_argument("--warm-refill-workers", type=int)
    parser.add_argument("--warm-wait-ms", type=int, default=0,
                        help="FIFO wait for an in-progress ready VM before cold fallback")
    parser.add_argument("--warm-idle-quiet-seconds", type=float, default=0)
    parser.add_argument("--warm-min-memory-mib", type=int, default=0)
    parser.add_argument("--warm-min-disk-gib", type=int, default=0)
    args=parser.parse_args()
    os.umask(0o077)
    if args.node_budget and args.admission_worker_socket:
        parser.error('Use Edge node admission or legacy worker admission, never both')
    if args.max_requests<1: parser.error("--max-requests must be positive")
    if args.snapshot_concurrency is not None and not 1 <= args.snapshot_concurrency <= args.capacity:
        parser.error("--snapshot-concurrency must be in 1..capacity")
    if args.warm_refill_workers is not None and not 1 <= args.warm_refill_workers <= args.capacity:
        parser.error("--warm-refill-workers must be in 1..capacity")
    if not 0 <= args.warm_wait_ms <= 10000:
        parser.error("--warm-wait-ms must be in 0..10000")
    if (not 0 <= args.warm_idle_quiet_seconds <= 60 or
            args.warm_min_memory_mib < 0 or args.warm_min_disk_gib < 0):
        parser.error("Invalid warm refill idle or resource floor")
    e3=None
    supplied=(args.e3_artifacts,args.e3_mixed_artifacts,args.e3_binary)
    if any(supplied) and not all(supplied):
        parser.error("E3 requires --e3-artifacts, --e3-mixed-artifacts and --e3-binary")
    if all(supplied):
        source=Path(args.e3_artifacts).resolve(strict=True)
        mixed=Path(args.e3_mixed_artifacts).resolve(strict=True)
        manifest=json.loads((mixed/"manifest.json").read_text())
        e3={"binary":Path(args.e3_binary).resolve(strict=True),
            "guest":(source/"guest/base.ext4").resolve(strict=True),
            "data":(source/"data.ext4").resolve(strict=True),
            "work_template":(mixed/"work-blank.ext4").resolve(strict=True),
            "data_sha256":manifest["data_image_sha256"]}
        for path,key in ((e3["binary"],"binary_sha256"),
                         (e3["guest"],"guest_image_sha256"),
                         (e3["data"],"data_image_sha256"),
                         (e3["work_template"],"work_blank_sha256"),
                         (Path(args.kernel),"kernel_sha256")):
            if sha(path)!=manifest[key]:
                parser.error(f"E3 artifact hash mismatch: {path}")
    from dsec.control.environments import load_configuration
    configuration=load_configuration(args, parser)
    tb2_templates=configuration.templates
    tb2_layers=configuration.layers
    tb2_layer_counts=configuration.layer_counts
    tb2_layer_transports=configuration.layer_transports
    tb2_layer_dax_indices=configuration.layer_dax_indices
    generic_dax_binaries=configuration.dax_binaries
    microvm_environment_catalog=configuration.catalog
    tb2_resources=configuration.resources
    warm_pool_specs=configuration.warm_pool_specs
    verifier_artifacts=configuration.verifier_artifacts
    dax_tasks=configuration.dax_tasks
    if sum(bool(value) for value in (args.tb2_network_tap,args.tb2_network_pool,
                                     args.tb2_netns_helper)) > 1:
        parser.error("Choose one TB2 network backend")
    tb2_network_manager=None
    if args.tb2_netns_helper:
        if not tb2_templates:
            parser.error("TB2 netns requires task templates")
        ipaddress.IPv4Address(args.tb2_netns_dns)
        if not Path(args.tb2_netns_helper).is_file():
            parser.error("TB2 netns helper missing")
        tb2_network_manager=NetnsNetworkManager(args.tb2_netns_helper,
                                                max_slots=args.tb2_netns_max_slots,
                                                dns=args.tb2_netns_dns,
                                                dax_binary=args.tb2_netns_dax_binary or args.tb2_verifier_dax_binary)
    tb2_network_slots={}
    if args.tb2_network_pool:
        if not tb2_templates:
            parser.error("TB2 network pool requires task templates")
        pool=json.loads(Path(args.tb2_network_pool).read_text())
        slots=pool.get("slots") if isinstance(pool,dict) else None
        if not isinstance(slots,list) or not slots or len(slots)>254:
            parser.error("TB2 network pool requires 1..254 slots")
        networks=[]
        for index,spec in enumerate(slots,1):
            try:
                tap=spec["tap"]
                if not isinstance(tap,str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,15}",tap):
                    raise ValueError("invalid TAP name")
                guest=ipaddress.IPv4Interface(spec["guest_cidr"])
                gateway=ipaddress.IPv4Address(spec["gateway"])
                dns=ipaddress.IPv4Address(spec["dns"])
                if (guest.network.prefixlen != 30 or gateway not in guest.network or
                        guest.ip in (guest.network.network_address,
                                     guest.network.broadcast_address) or
                        gateway == guest.ip or gateway in (guest.network.network_address,
                                                            guest.network.broadcast_address)):
                    raise ValueError("slot requires distinct guest/gateway in one /30")
                if any(guest.network.overlaps(network) for network in networks):
                    raise ValueError("overlapping /30 slots")
                if tap in tb2_network_slots:
                    raise ValueError("duplicate TAP")
                address=subprocess.check_output(["ip","-4","-o","addr","show","dev",tap],
                                                text=True)
                if f" {gateway}/30 " not in address:
                    raise ValueError("TAP gateway address mismatch")
            except (KeyError,TypeError,ValueError,subprocess.CalledProcessError) as exc:
                parser.error(f"Invalid TB2 network slot {index}: {exc}")
            networks.append(guest.network)
            tb2_network_slots[tap]={"tap":tap,"guest_cidr":str(guest),
                                    "gateway":str(gateway),"dns":str(dns),
                                    "mac":f"06:00:ac:1e:70:{index:02x}"}
    if args.tb2_network_tap:
        if (not tb2_templates or args.capacity != 1 or
                not re.fullmatch(r"[a-zA-Z0-9_-]{1,15}", args.tb2_network_tap) or
                not (Path("/sys/class/net") / args.tb2_network_tap).exists()):
            parser.error("Networked TB2 requires a configured TAP and capacity=1")
    overlaybd_root_store=None
    if configuration.roots:
        if not all((args.overlaybd_ublk_socket, args.overlaybd_global_config)):
            parser.error("OverlayBD roots require ublk socket and global config")
        roots=configuration.roots
        from dsec.storage.overlaybd import OverlayBDRootStore
        overlaybd_root_store=OverlayBDRootStore(
            args.overlaybd_ublk_socket, args.overlaybd_global_config, roots,
            permission_container=args.overlaybd_permission_container)
        overlaybd_root_store.client.get_features()
    if args.validate_only:
        print(json.dumps({"status": "passed", "environment_count": len(tb2_templates),
                          "catalog_sha256": microvm_environment_catalog.digest if
                          microvm_environment_catalog else None}))
        return
    from dsec.runtime.registry import atomic_json, identity
    from dsec.runtime.edge import open_edge
    node_admission=None
    manager=None
    server=None
    container_runtime=None
    sock=Path(args.root).resolve()/"service.sock"
    try:
        if args.node_budget:
            from dataclasses import fields
            from dsec.contracts.resources import NodeBudget, NodeDemand
            from dsec.runtime.resources import ProcHostSampler
            from dsec.runtime.node_admission import NodeAdmission
            settings=json.loads(Path(args.node_budget).read_text())
            budget=NodeBudget(**{field.name:settings[field.name] for field in fields(NodeBudget)
                                if field.name in settings})
            sampler=ProcHostSampler(settings.get('disk_path', args.root),
                                    settings['network_interface'], settings.get('disk_device'))
            node_admission=NodeAdmission(args.root, budget, sampler,
                default_demand=NodeDemand(**settings['node_default_demand'])
                               if 'node_default_demand' in settings else None,
                ready_demand=NodeDemand(**settings['node_ready_demand'])
                             if 'node_ready_demand' in settings else None)
        manager=open_edge(args.root,args.binary,args.kernel,args.template,
                               capacity=args.capacity,e3=e3,tb2_templates=tb2_templates,
                               tb2_layers=tb2_layers,
                               tb2_layer_counts=tb2_layer_counts,
                               tb2_layer_transports=tb2_layer_transports,
                               tb2_layer_dax_indices=tb2_layer_dax_indices,
                               generic_dax_binaries=generic_dax_binaries,
                               microvm_environment_catalog=microvm_environment_catalog,
                               overlaybd_root_store=overlaybd_root_store,
                               tb2_resources=tb2_resources,
                               tb2_free_page_reporting=args.tb2_free_page_reporting,
                               tb2_network_tap=args.tb2_network_tap,
                               tb2_network_slots=tb2_network_slots,
                               tb2_network_manager=tb2_network_manager,
                               tb2_verifier_artifacts=verifier_artifacts,
                               tb2_verifier_dax_tasks=dax_tasks,
                               tb2_verifier_dax_binary=args.tb2_verifier_dax_binary,
                               snapshot_cache_policy=args.snapshot_cache_policy,
                               snapshot_concurrency=args.snapshot_concurrency,
                               snapshot_strategy=args.snapshot_strategy,
                               snapshot_editor=args.snapshot_editor,
                               warm_pool_specs=warm_pool_specs,
                               warm_refill_workers=args.warm_refill_workers,
                               warm_wait_ms=args.warm_wait_ms,
                               warm_idle_quiet_seconds=args.warm_idle_quiet_seconds,
                               warm_min_memory_mib=args.warm_min_memory_mib,
                               warm_min_disk_gib=args.warm_min_disk_gib,
                               egress_proxy_url=args.egress_proxy_url,
                               egress_proxy_bypass_hosts=args.egress_proxy_bypass_host,
                               node_admission=node_admission)
        journal=RequestJournal(args.root)
        sock=Path(args.root).resolve()/"service.sock"
        # Only the manager that holds the directory lock may replace this socket.
        sock.unlink(missing_ok=True)
        server=BoundedServer(str(sock),Handler,args.max_requests)
        os.chmod(sock,int(args.socket_mode,8)); server.manager=manager; server.journal=journal
        server.admission_worker_socket=args.admission_worker_socket
        server.identity_reader=identity
        server.node_admission=node_admission
        container_runtime=ContainerRuntime(admission_worker_socket=args.admission_worker_socket,
                                                 node_admission=node_admission)
        server.container_runtime=container_runtime
        container_runtime.reconcile_node_leases()
        if node_admission is not None:
            node_admission.activate()
        def shutdown(*_):
            threading.Thread(target=server.shutdown,daemon=True).start()
        signal.signal(signal.SIGTERM,shutdown); signal.signal(signal.SIGINT,shutdown)
        atomic_json(Path(args.root).resolve()/"daemon.json",identity(os.getpid()))
        print("READY "+str(sock),flush=True)
        server.serve_forever(poll_interval=.1)
    finally:
        if server is not None:
            server.server_close()
            sock.unlink(missing_ok=True)
        if container_runtime is not None:
            container_runtime.close()
        if manager is not None:
            (Path(args.root).resolve()/"daemon.json").unlink(missing_ok=True)
            manager.detach()
        if node_admission is not None:
            node_admission.close()

if __name__=="__main__":
    main()
