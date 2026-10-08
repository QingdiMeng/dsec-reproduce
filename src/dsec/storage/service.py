"""One runtime storage entry point over existing file and OverlayBD components.

This service owns no sandbox state, journal, slot or lease. Inputs are storage
paths and existing backend handles. The caller records acquired device IDs
before later identity checks/persistence, including failures.
"""
from pathlib import Path
import shutil

from dsec.contracts.errors import SandboxError
from dsec.contracts.storage import DiskPaths, DiskStorageCapabilities


class RuntimeStorage:
    def __init__(self, *, copy_sparse, snapshot_hash, hash_file):
        self.copy_sparse = copy_sparse
        self.snapshot_hash = snapshot_hash
        self.hash_file = hash_file

    def capabilities(self, store=None, *, snapshot_mode="full"):
        # These are disk mechanism capabilities; runtime still checks quiescence,
        # isolation and profile compatibility before admitting a prepared fork.
        return DiskStorageCapabilities("overlaybd-ublk" if store is not None else "file-ext4",
                                       prepared_fork=snapshot_mode == "full")

    def prepare(self, catalog, environment_id, source):
        # Existing catalog performs source, integrity, DAX and backend validation.
        return catalog.resolve(environment_id, source)

    def copy_file(self, source, destination, *, sparse=False, mode=None):
        if sparse:
            self.copy_sparse(source, destination)
        else:
            shutil.copy2(source, destination)
        if mode is not None:
            destination.chmod(mode)

    def create_writable(self, paths: DiskPaths, source, *, sparse=False, store=None, image=None):
        if store:
            # Do not query socket identity here: the caller must first retain
            # the acquired device ID/runtime so later failures can clean it up.
            return store.create(image, paths.directory, paths.root)
        self.copy_file(source, paths.root, sparse=sparse, mode=0o600)
        return None

    def restore_disk(self, paths, snapshot, *, sparse=False, store=None):
        return self.create_writable(paths, snapshot/"disk.ext4", sparse=sparse,
                                    store=store, image=snapshot/"disk-image.json")

    def checkpoint_disk(self, paths, staging, target, *, sparse=False, store=None,
                        image=None, device_id=None):
        latest = None
        if store:
            latest = store.snapshot(device_id, image, staging, target)
        else:
            self.copy_file(paths.root, staging/"disk.ext4", sparse=sparse)
        if paths.work:
            self.copy_file(paths.work, staging/"work.ext4")
        return latest

    def verify_checkpoint(self, paths, snapshot, manifest, *, store=None, source_image=None):
        overlaybd = store is not None
        if manifest.get("rootfs_block_backend", "file-ext4") != self.capabilities(store).root_backend:
            raise SandboxError("Snapshot block backend mismatch")
        files = (("memory", "state", "disk-image.json") if overlaybd else
                 ("memory", "state", "disk.ext4")) + (("work.ext4",) if paths.work else ())
        if overlaybd:
            disk_layers = store.disk_layers(snapshot/"disk-image.json", paths.directory, source_image)
            expected_layers = {path.name for path in disk_layers}
            if (len(expected_layers) != len(disk_layers) or
                    set(manifest.get("disk_layers", {})) != expected_layers):
                raise SandboxError("Snapshot disk layer list mismatch")
            for path in disk_layers:
                if self.hash_file(path) != manifest["disk_layers"][path.name]:
                    raise SandboxError("Snapshot disk layer integrity mismatch: "+path.name)
        for name in files:
            algorithm = manifest.get("hash_algorithms", {}).get(name, "sha256")
            if self.snapshot_hash(snapshot/name, algorithm) != manifest["files"][name]:
                raise SandboxError("Snapshot integrity mismatch: "+name)

    def service_matches(self, store, device_id, saved_identity):
        if not store or device_id is None:
            return True
        try:
            return saved_identity == store.socket_identity()
        except (OSError, RuntimeError, ValueError, IndexError):
            return False

    def device_identity(self, store):
        return store.socket_identity()

    def release_device(self, store, device_id, runtime, directory, saved_identity):
        if not store or device_id is None:
            return
        device_present = (Path(f"/sys/block/ublkb{device_id}").exists() or
                          Path(f"/dev/ublkb{device_id}").exists())
        if not device_present:
            store.delete(None, runtime, directory)
        elif self.service_matches(store, device_id, saved_identity):
            store.delete(device_id, runtime, directory)
        else:
            raise SandboxError("Old ublk device still exists after daemon change")

    def release_writable(self, paths):
        paths.root.unlink(missing_ok=True)
        if paths.work:
            paths.work.unlink(missing_ok=True)

    def remove_checkpoint(self, path):
        # Caller determines whether this is committed/unpublished and retains
        # fork pins; storage never decides a lifecycle or generation transition.
        shutil.rmtree(path)

    def release_layers(self, directory):
        layers = directory/"disk-layers"
        if layers.exists():
            shutil.rmtree(layers)
