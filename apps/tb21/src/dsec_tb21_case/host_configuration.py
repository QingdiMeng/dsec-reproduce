"""TB2.1 legacy CLI manifest validation; no VM or service effects."""
import json
from pathlib import Path
import re
import tomllib
from dsec.storage.digest import sha
from .verifier_artifact import VerifierArtifactStore


def templates(args, parser, config):
    tb2_templates=config.templates
    for spec in args.tb2_template:
        task_id, separator, path = spec.partition("=")
        if not separator or not task_id or not path or not all(
                char.islower() or char.isdigit() or char in "-." for char in task_id):
            parser.error("--tb2-template requires task-id=path")
        environment_id="tb2-"+task_id
        if environment_id in tb2_templates:
            parser.error("Duplicate TB2 task template")
        tb2_templates[environment_id]=Path(path).resolve(strict=True)


def validate_catalog(args, parser, catalog):
    if any(name.startswith("tb2-") for name in catalog.entries) and not args.tb2_manifest:
        parser.error("Catalogued TB2 environments require a pinned task manifest")


def configure(args, parser, config):
    tb2_templates=config.templates
    tb2_layers=config.layers
    tb2_layer_counts=config.layer_counts
    tb2_layer_transports=config.layer_transports
    tb2_layer_dax_indices=config.layer_dax_indices
    generic_resources=config.catalog_resources
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
    warm_pool_specs=config.warm_pool_specs
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
    tb2_resources=config.resources
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
    config.verifier_artifacts=verifier_artifacts
    config.dax_tasks=dax_tasks
    for spec in args.tb2_overlaybd_root:
        task_id, separator, image = spec.partition("=")
        environment_id="tb2-"+task_id
        if not separator or environment_id not in tb2_templates or environment_id in config.roots:
            parser.error("OverlayBD root requires a unique configured TB2 task")
        config.roots[environment_id]=Path(image).resolve(strict=True)
