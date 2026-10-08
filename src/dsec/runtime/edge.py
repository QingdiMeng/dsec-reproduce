"""Formal single-node runtime assembly by composition, with a legacy initializer."""
from pathlib import Path

from dsec.runtime.lifecycle import SandboxManager


def initialize_edge(manager, root, binary, kernel, template, *, registry=None, **options):
    """Acquire ownership, recover records, then start admission/refill monitors."""
    root = Path(root).resolve()
    if registry is None:
        from dsec.runtime.registry import create_registry
        registry = create_registry(root)
    initialized = False
    try:
        registry.require_owner()
        if registry.root != root:
            raise ValueError("Registry root disagrees with Edge root")
        SandboxManager.__init__(manager, root, Path(binary).resolve(),
            Path(kernel).resolve(), Path(template).resolve(),
            start_monitor=False, registry=registry, **options)
        initialized = True
        registry.recover(manager)
        manager.start_monitors()
        return manager
    except BaseException:
        # Failed assembly must retire threads without terminating attested live
        # sandboxes; their records/leases remain the recovery evidence.
        try:
            if initialized:
                manager._retire_threads()
                registry.release_handles(manager)
        finally:
            registry.close()
        raise


def open_edge(root, binary, kernel, template, *, registry=None, **options):
    return initialize_edge(SandboxManager.__new__(SandboxManager), root, binary,
                           kernel, template, registry=registry, **options)
