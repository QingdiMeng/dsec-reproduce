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
import tomllib
from dsec.runtime.lifecycle import SandboxError
from dsec.storage.digest import sha
from dsec.runtime.isolation.network import NetnsNetworkManager
from tb2_verifier_artifact import VerifierArtifactStore
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
    tb2_templates={}
    for spec in args.tb2_template:
        task_id, separator, path = spec.partition("=")
        if not separator or not task_id or not path or not all(
                char.islower() or char.isdigit() or char in "-." for char in task_id):
            parser.error("--tb2-template requires task-id=path")
        environment_id="tb2-"+task_id
        if environment_id in tb2_templates:
            parser.error("Duplicate TB2 task template")
        tb2_templates[environment_id]=Path(path).resolve(strict=True)
    tb2_layers={}
    tb2_layer_counts={}
    tb2_layer_transports={}
    tb2_layer_dax_indices={}
    generic_resources={}
    generic_roots={}
    generic_dax_binaries={}
    microvm_environment_catalog=None
    if args.microvm_environment_catalog:
        from dsec.storage.catalog import MicroVMEnvironmentCatalog
        catalog=MicroVMEnvironmentCatalog(args.microvm_environment_catalog)
        microvm_environment_catalog=catalog
        if any(name.startswith("tb2-") for name in catalog.entries) and not args.tb2_manifest:
            parser.error("Catalogued TB2 environments require a pinned task manifest")
        for environment_id, item in catalog.entries.items():
            if item.get("backend") != "microvm" or environment_id in tb2_templates or \
                    environment_id in ("default", "e3-mixed"):
                parser.error(f"Invalid or duplicate generic microVM environment: {environment_id}")
            try:
                resolved=catalog.resolve(environment_id)
            except (OSError, ValueError) as exc:
                parser.error(f"Invalid generic microVM environment {environment_id}: {exc}")
            tb2_templates[environment_id]=resolved["boot_template"] or Path(args.template).resolve(strict=True)
            tb2_layers[environment_id]=[layer["file"] for layer in resolved["layers"]]
            tb2_layer_counts[environment_id]=len(resolved["layers"])
            tb2_layer_transports[environment_id]=("virtio-erofs-dax" if
                resolved["erofs_dax_indices"] else "virtio-erofs-layers")
            if resolved["erofs_dax_indices"]:
                tb2_layer_dax_indices[environment_id]=resolved["erofs_dax_indices"]
                generic_dax_binaries[environment_id]=resolved["dax_binary"]
            generic_resources[environment_id]={"cpus":resolved["cpus"],
                                               "memory_mb":resolved["memory_mb"],
                                               "command_timeout_ms":resolved["command_timeout_ms"]}
            if resolved["root_block_backend"] == "overlaybd-ublk":
                generic_roots[environment_id]=resolved["overlaybd_root_image"]
    for entry in args.tb2_layer_manifest:
        task_id, separator, path = entry.partition("=")
        environment_id="tb2-"+task_id
        if (not separator or environment_id not in tb2_templates or
                environment_id in tb2_layers):
            parser.error("Layer manifest requires a unique configured TB2 task")
        layer_spec=json.loads(Path(path).read_text())
        if layer_spec.get("format") == 2:
            if (layer_spec.get("transport") != "fuse-erofs-bundle" or
                    layer_spec.get("task_id") != task_id):
                parser.error("Invalid FUSE EROFS bundle transport")
            source_path=Path(layer_spec["source_manifest"]).resolve(strict=True)
            map_path=Path(layer_spec["bundle_map"]).resolve(strict=True)
            if (sha(source_path) != layer_spec.get("source_manifest_sha256") or
                    sha(map_path) != layer_spec.get("bundle_map_sha256")):
                parser.error("EROFS bundle source or map hash mismatch")
            source_spec=json.loads(source_path.read_text())
            bundle_map=json.loads(map_path.read_text())
            source_layers=source_spec.get("layers", [])
            spans=bundle_map.get("layers", [])
            if (source_spec.get("format") != 1 or
                    source_spec.get("image_id") != layer_spec.get("image_id") or
                    bundle_map.get("image_id") != layer_spec.get("image_id") or
                    len(source_layers) != len(spans) or
                    len(spans) != layer_spec.get("logical_layer_count") or
                    not spans):
                parser.error("EROFS bundle layer list differs from source image")
            for original, span in zip(source_layers, spans):
                if (original.get("erofs_sha256") != span.get("erofs_sha256") or
                        original.get("erofs_bytes") != span.get("length_bytes") or
                        original.get("erofs") != span.get("erofs")):
                    parser.error("EROFS bundle span differs from source layer")
            bundle_disk=Path(layer_spec["bundle_disk"]).resolve(strict=True)
            if bundle_disk.stat().st_size != layer_spec.get("bundle_disk_bytes"):
                parser.error("EROFS bundle virtual disk size mismatch")
            tb2_layers[environment_id]=[bundle_disk]
            tb2_layer_counts[environment_id]=len(source_layers)
            tb2_layer_transports[environment_id]="fuse-erofs-bundle"
            continue
        layers=layer_spec.get("layers")
        if (layer_spec.get("format") != 1 or
                not isinstance(layers,list) or not 1 <= len(layers) <= 17):
            parser.error("Invalid EROFS layer manifest")
        paths=[]
        dax_indices=[]
        for index, layer in enumerate(layers):
            if (not isinstance(layer,dict) or
                    not re.fullmatch(r"[0-9a-f]{64}", layer.get("erofs_sha256", "")) or
                    not isinstance(layer.get("erofs_bytes"),int)):
                parser.error("Invalid EROFS layer entry")
            artifact=Path(layer["erofs"]).resolve(strict=True)
            if artifact.stat().st_size != layer["erofs_bytes"]:
                parser.error(f"EROFS layer size mismatch: {artifact}")
            if layer.get("dax"):
                if (layer.get("dax") is not True or
                        layer["erofs_bytes"] % (2 * 1024 * 1024) or
                        sha(artifact) != layer["erofs_sha256"]):
                    parser.error("DAX EROFS layer must be pinned and 2 MiB aligned")
                dax_indices.append(index)
            paths.append(artifact)
        compaction=layer_spec.get("compaction")
        logical_count=len(paths)
        transport="virtio-erofs-layers"
        if compaction is not None:
            source_path=Path(layer_spec["source_manifest"]).resolve(strict=True)
            if sha(source_path)!=layer_spec.get("source_manifest_sha256"):
                parser.error("Collapsed EROFS source manifest hash mismatch")
            original=json.loads(source_path.read_text())
            groups=compaction.get("groups")
            source_layers=original.get("layers", [])
            if (compaction.get("format")!=1 or original.get("format")!=1 or
                    original.get("image_id")!=layer_spec.get("image_id") or
                    compaction.get("source_layer_count")!=len(source_layers) or
                    compaction.get("physical_layer_count")!=len(paths) or
                    not isinstance(groups,list) or len(groups)!=len(paths) or
                    any(not isinstance(group,list) or not group or
                        any(not isinstance(i,int) for i in group) for group in groups) or
                    [i for group in groups for i in group]!=list(range(len(source_layers)))):
                parser.error("Collapsed EROFS layer provenance differs from source")
            for group, layer, artifact in zip(groups,layers,paths):
                if len(group)==1:
                    original_layer=source_layers[group[0]]
                    if layer.get("dax"):
                        if (layer.get("dax_source_erofs_sha256") !=
                                original_layer.get("erofs_sha256")):
                            parser.error("DAX EROFS source layer differs")
                    elif (layer.get("erofs_sha256")!=original_layer.get("erofs_sha256") or
                          layer.get("erofs_bytes")!=original_layer.get("erofs_bytes") or
                          artifact!=Path(original_layer["erofs"]).resolve(strict=True)):
                        parser.error("Unchanged EROFS layer differs from source")
                    continue
                build_path=Path(layer["build_manifest"]).resolve(strict=True)
                build=json.loads(build_path.read_text())
                if (layer.get("collapsed_from")!=group or
                        build.get("source_layer_indices")!=group or
                        build.get("source_erofs_sha256")!=[
                            source_layers[i]["erofs_sha256"] for i in group] or
                        build.get("mkfs_erofs_sha256")!=compaction.get("mkfs_erofs_sha256") or
                        build.get("erofs_sha256")!=layer.get("erofs_sha256") or
                        build.get("erofs_bytes")!=layer.get("erofs_bytes") or
                        Path(build.get("erofs", "")).resolve(strict=True)!=artifact or
                        sha(artifact)!=layer["erofs_sha256"]):
                    parser.error("Collapsed EROFS artifact provenance or hash mismatch")
            logical_count=len(source_layers)
            transport="virtio-erofs-collapsed"
        if dax_indices:
            transport="virtio-erofs-dax"
            tb2_layer_dax_indices[environment_id]=tuple(dax_indices)
        tb2_layers[environment_id]=paths
        tb2_layer_counts[environment_id]=logical_count
        tb2_layer_transports[environment_id]=transport
    warm_pool_specs={}
    for spec in args.warm_pool:
        parts=spec.split(":")
        if len(parts)!=3 or not parts[2].isdigit() or int(parts[2])<1:
            parser.error("--warm-pool requires task-id:verifier-storage:positive-target")
        environment_id="tb2-"+parts[0]
        storage=None if parts[1]=="none" else parts[1]
        key=(environment_id,storage)
        if environment_id not in tb2_templates or key in warm_pool_specs:
            parser.error("Warm pool requires a unique configured TB2 task")
        warm_pool_specs[key]=int(parts[2])
    if sum(warm_pool_specs.values())>args.capacity:
        parser.error("Warm pool targets exceed capacity")
    tb2_resources=dict(generic_resources)
    verifier_artifacts=None
    if args.tb2_verifier_artifact_manifest or args.tb2_verifier_artifact_local or args.tb2_verifier_artifact_threefs:
        if (not tb2_templates or not args.tb2_verifier_artifact_manifest or
                not args.tb2_verifier_artifact_local):
            parser.error("TB2 verifier artifact requires task templates, manifest and local path")
        verifier_artifacts=VerifierArtifactStore(
            args.tb2_verifier_artifact_manifest, args.tb2_verifier_artifact_local,
            args.tb2_verifier_artifact_threefs)
    if args.tb2_task_verifier_artifact:
        stores={"default":verifier_artifacts} if verifier_artifacts else {}
        for spec in args.tb2_task_verifier_artifact:
            parts=spec.split("=")
            if len(parts)!=3 or "tb2-"+parts[0] not in tb2_templates:
                parser.error("Task verifier artifact requires configured task-id=manifest=local-ext4")
            environment_id="tb2-"+parts[0]
            if environment_id in stores:
                parser.error("Duplicate task verifier artifact")
            stores[environment_id]=VerifierArtifactStore(parts[1],parts[2])
        verifier_artifacts=stores
    dax_tasks={"tb2-" + task_id for task_id in args.tb2_verifier_dax_task}
    if len(dax_tasks) != len(args.tb2_verifier_dax_task):
        parser.error("Duplicate DAX verifier task")
    if args.tb2_manifest:
        manifest=json.loads(Path(args.tb2_manifest).read_text())
        tasks=manifest.get("tasks")
        if not isinstance(tasks,dict):
            parser.error("--tb2-manifest lacks a tasks map")
        for environment_id in tb2_templates:
            if environment_id in generic_resources and not environment_id.startswith("tb2-"):
                continue
            task_id=environment_id.removeprefix("tb2-")
            spec=tasks.get(task_id)
            if not isinstance(spec,dict):
                parser.error(f"TB2 task missing from manifest: {task_id}")
            cpus,memory_mb=spec.get("cpus"),spec.get("memory_mb")
            if (not isinstance(cpus,int) or isinstance(cpus,bool) or not 1<=cpus<=32 or
                    not isinstance(memory_mb,int) or isinstance(memory_mb,bool) or
                    not 512<=memory_mb<=32768):
                parser.error(f"Invalid TB2 resources: {task_id}")
            task_toml=Path(args.tb2_manifest).parent/"tasks"/task_id/"task.toml"
            if not task_toml.is_file() or sha(task_toml)!=spec.get("task_toml_sha256"):
                parser.error(f"Pinned TB2 task config mismatch: {task_id}")
            verifier_timeout=tomllib.loads(task_toml.read_text()).get("verifier", {}).get(
                "timeout_sec", 900)
            if (not isinstance(verifier_timeout,(int,float)) or
                    not 1<=verifier_timeout<=12000 or int(verifier_timeout)!=verifier_timeout):
                parser.error(f"Invalid TB2 verifier timeout: {task_id}")
            # Catalogued task environments retain their declared VM limits;
            # the task manifest still pins the official verifier deadline.
            limits = tb2_resources.setdefault(environment_id, {})
            if environment_id not in generic_resources:
                limits.update(cpus=cpus, memory_mb=memory_mb)
            limits["verifier_timeout_ms"] = int(verifier_timeout)*1000
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
    if args.tb2_overlaybd_root or generic_roots:
        if not all((args.overlaybd_ublk_socket, args.overlaybd_global_config)):
            parser.error("OverlayBD roots require ublk socket and global config")
        roots=dict(generic_roots)
        for spec in args.tb2_overlaybd_root:
            task_id, separator, image = spec.partition("=")
            environment_id="tb2-"+task_id
            if not separator or environment_id not in tb2_templates or environment_id in roots:
                parser.error("OverlayBD root requires a unique configured TB2 task")
            roots[environment_id]=Path(image).resolve(strict=True)
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
    from dsec.runtime.registry import DurableManager, atomic_json, identity
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
        manager=DurableManager(args.root,args.binary,args.kernel,args.template,
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
