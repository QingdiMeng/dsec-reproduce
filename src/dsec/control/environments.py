"""Validate generic microVM catalogs and opt-in application configuration."""
from dataclasses import dataclass, field
from pathlib import Path
from dsec.storage.catalog import MicroVMEnvironmentCatalog
from dsec.compat.applications import require_tb21


# Historical flags are compatibility entry points, not task implementations.
TB21_FLAGS = ("tb2_template", "tb2_layer_manifest", "tb2_manifest", "tb2_overlaybd_root",
              "tb2_verifier_artifact_manifest", "tb2_verifier_artifact_local",
              "tb2_verifier_artifact_threefs", "tb2_task_verifier_artifact",
              "tb2_verifier_dax_task", "warm_pool")


@dataclass
class MicroVMConfiguration:
    templates: dict = field(default_factory=dict)
    layers: dict = field(default_factory=dict)
    layer_counts: dict = field(default_factory=dict)
    layer_transports: dict = field(default_factory=dict)
    layer_dax_indices: dict = field(default_factory=dict)
    dax_binaries: dict = field(default_factory=dict)
    catalog_resources: dict = field(default_factory=dict)
    resources: dict = field(default_factory=dict)
    roots: dict = field(default_factory=dict)
    warm_pool_specs: dict = field(default_factory=dict)
    verifier_artifacts: object = None
    dax_tasks: set = field(default_factory=set)
    catalog: object = None


def load_configuration(args, parser):
    config = MicroVMConfiguration()
    application = None
    if any(getattr(args, flag, None) for flag in TB21_FLAGS):
        application = require_tb21("host_configuration")
        application.templates(args, parser, config)
    if args.microvm_environment_catalog:
        catalog = MicroVMEnvironmentCatalog(args.microvm_environment_catalog)
        config.catalog = catalog
        if any(name.startswith("tb2-") for name in catalog.entries):
            application = application or require_tb21("host_configuration")
            application.validate_catalog(args, parser, catalog)
        for environment_id, item in catalog.entries.items():
            if item.get("backend") != "microvm" or environment_id in config.templates or \
                    environment_id in ("default", "e3-mixed"):
                parser.error(f"Invalid or duplicate generic microVM environment: {environment_id}")
            try:
                resolved=catalog.resolve(environment_id)
            except (OSError, ValueError) as exc:
                parser.error(f"Invalid generic microVM environment {environment_id}: {exc}")
            config.templates[environment_id]=resolved["boot_template"] or Path(args.template).resolve(strict=True)
            config.layers[environment_id]=[layer["file"] for layer in resolved["layers"]]
            config.layer_counts[environment_id]=len(resolved["layers"])
            config.layer_transports[environment_id]=("virtio-erofs-dax" if
                resolved["erofs_dax_indices"] else "virtio-erofs-layers")
            if resolved["erofs_dax_indices"]:
                config.layer_dax_indices[environment_id]=resolved["erofs_dax_indices"]
                config.dax_binaries[environment_id]=resolved["dax_binary"]
            config.catalog_resources[environment_id]={"cpus":resolved["cpus"],
                                               "memory_mb":resolved["memory_mb"],
                                               "command_timeout_ms":resolved["command_timeout_ms"]}
            if resolved["root_block_backend"] == "overlaybd-ublk":
                config.roots[environment_id]=resolved["overlaybd_root_image"]

    config.resources = dict(config.catalog_resources)
    if application is not None:
        application.configure(args, parser, config)
    return config
