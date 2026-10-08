"""Same-host prepared-state forks; an explicit extension of DSec pause/resume.

A sealed source remains a paused, TTL-bound sandbox. Branches use private
writable disks and MAP_PRIVATE guest memory from its immutable checkpoint.
Only trusted environment preparation (before policy dialogue) may be sealed.
Active TCP sessions are rejected; arbitrary application PRNGs are not reset.
"""
import json
import math
import os
from pathlib import Path
import re
import shlex
import time
import shutil
import uuid


def _python(source):
    return "PATH=/usr/local/bin:/usr/bin:/bin:$PATH python3 -c " + shlex.quote(source)


QUIESCENCE = _python('''from pathlib import Path
for filename in ('tcp', 'tcp6'):
    for row in Path('/proc/net/' + filename).read_text().splitlines()[1:]:
        if row.split()[3] not in ('0A', '06'):
            raise RuntimeError('Prepared baseline has a live TCP connection')
print('prepared-state-ready')
''')


def identity_command(sandbox_id):
    if not re.fullmatch(r"[0-9a-f]{12}", sandbox_id):
        raise ValueError("Invalid branch identity")
    # RNDRESEEDCRNG after fresh host entropy, rather than cloning kernel RNG state.
    entropy = os.urandom(64).hex()
    return _python(f'''import fcntl, os, struct
from pathlib import Path
seed=bytes.fromhex('{entropy}')
fd=os.open('/dev/random', os.O_RDWR)
try:
    fcntl.ioctl(fd, 0x40085203, struct.pack('ii', len(seed)*8, len(seed))+seed)
    fcntl.ioctl(fd, 0x5207, 0)
finally:
    os.close(fd)
identity='{sandbox_id}'
Path('/run/dsec-episode-id').write_text(identity+'\\n')
Path('/etc/machine-id').write_text(identity+'0'*20+'\\n')
print(identity)
''')


def seal_baseline(sb, *, allow_prepared_state=False):
    from dsec.runtime.lifecycle import SandboxError
    with sb.lock:
        if allow_prepared_state is not True:
            raise ValueError("Sealing requires explicit allow_prepared_state=True")
        sb._check()
        if sb.baseline_sealed:
            if sb.state != "PAUSED":
                raise SandboxError("Baseline is no longer available")
            return sb.status()
        if sb.state != "RUNNING" or sb.reserved or sb.snapshot_mode != "full":
            raise SandboxError("Sealing requires an unreserved RUNNING full-snapshot sandbox")
        if sb.network_mode not in (None, "netns"):
            raise SandboxError("Prepared-state forks require isolated netns networking")
        check = sb.vm.execute(QUIESCENCE)
        if check["exit_code"] != 0 or check.get("timed_out"):
            raise SandboxError("Guest preparation is not quiescent: " + check.get("output", ""))
        sb.pause()
        _publish_prepared_snapshot(sb)
        _validate_snapshot(sb)
        sb.baseline_identity = _file_identities(sb)
        sb.baseline_verified = True
        sb.baseline_sealed = True
        sb._persist()
        return sb.status()


def _validate_snapshot(source):
    from dsec.runtime.lifecycle import SandboxError, _snapshot_hash
    from dsec.storage.digest import sha
    manifest = json.loads((source.snapshot / "manifest.json").read_text())
    if (manifest['id'] != source.id or manifest['generation'] != source.generation or
            manifest['snapshot_mode'] != 'full' or
            manifest['binary_sha256'] != sha(Path(source.vm.binary)) or
            manifest['guest_kernel'] != str(source.kernel) or
            manifest['environment_manifest_sha256'] != source.environment_manifest_sha256):
        raise SandboxError("Baseline identity changed")
    for name, digest in manifest['files'].items():
        if name not in ('state', 'memory', 'disk.ext4', 'disk-image.json', 'work.ext4'):
            raise SandboxError("Invalid baseline file")
        if _snapshot_hash(source.snapshot/name,
                          manifest.get('hash_algorithms', {}).get(name, 'sha256')) != digest:
            raise SandboxError("Baseline integrity mismatch: " + name)
    if source.overlaybd_store:
        layers = source.overlaybd_store.disk_layers(source.snapshot/'disk-image.json',
                    source.directory, source.overlaybd_store.source_for(source.environment_id))
        if {p.name: sha(p) for p in layers} != manifest['disk_layers']:
            raise SandboxError("Baseline disk layer integrity mismatch")
    return manifest


def _file_identities(source):
    """Trusted local immutable artifacts: detect replacement, rewrite and chmod."""
    paths = [source.snapshot/name for name in
             json.loads((source.snapshot/'manifest.json').read_text())['files']]
    paths += [source.snapshot/'manifest.json', Path(source.vm.binary), Path(source.kernel)]
    if source.overlaybd_store:
        paths += source.overlaybd_store.disk_layers(source.snapshot/'disk-image.json',
                   source.directory, source.overlaybd_store.source_for(source.environment_id))
    identities = {}
    for path in paths:
        stat = path.stat()
        if path.is_symlink() or not path.is_file():
            raise ValueError('Invalid prepared artifact: '+str(path))
        identities[str(path)] = [stat.st_dev, stat.st_ino, stat.st_size,
                                 stat.st_mtime_ns, stat.st_ctime_ns]
    return identities


def _publish_prepared_snapshot(source):
    """Publish shared disk lowers and a new snapshot atomically via registry.

    Keep the old checkpoint complete until the new registry is committed. Disk
    layers are copied only once here; forks subsequently hold their shared CAS
    paths. Memory/state files are hardlinked (owned by the manager), not copied.
    """
    if not source.overlaybd_store:
        return
    from dsec.runtime.lifecycle import _fsync_directory
    from dsec.storage.digest import sha
    store = source.overlaybd_store
    shared = store.shared_layers
    old = source.snapshot
    manifest = json.loads((old/'manifest.json').read_text())
    image = json.loads((old/'disk-image.json').read_text())
    layers = store.disk_layers(old/'disk-image.json', source.directory,
                              store.source_for(source.environment_id))
    shared_paths = []
    latest = manifest.get('latest_disk_layer')
    for layer in layers:
        if shared.owns(layer):
            shared.hold(source.id, [layer])
            target = layer
        else:
            target = shared.publish_and_hold(source.id, layer, manifest['disk_layers'][layer.name])
        shared_paths.append(target)
        if layer.name == latest:
            manifest['latest_disk_layer'] = target.name
        for lower in image['lowers']:
            if lower['file'] == str(layer):
                lower['file'] = str(target)
    staging = source.directory/('pending-'+uuid.uuid4().hex[:8])
    staging.mkdir(mode=0o700)
    store.share_directory(staging)
    target = source.directory/f'snapshot-{source.generation+1}'
    published = False
    try:
        for name in manifest['files']:
            if name != 'disk-image.json':
                os.link(old/name, staging/name)
        (staging/'disk-image.json').write_text(json.dumps(image, sort_keys=True)+'\n')
        (staging/'disk-image.json').chmod(0o660)
        manifest['generation'] = source.generation+1
        manifest['disk_layers'] = {p.name:p.stem for p in shared_paths}
        manifest['files']['disk-image.json'] = sha(staging/'disk-image.json')
        (staging/'manifest.json').write_text(json.dumps(manifest, sort_keys=True)+'\n')
        for name in ('disk-image.json', 'manifest.json'):
            with (staging/name).open('rb') as stream:
                os.fsync(stream.fileno())
        _fsync_directory(staging)
        staging.rename(target)
        _fsync_directory(source.directory)
        source.snapshot = target
        source.generation += 1
        source.overlaybd_image = target/'disk-image.json'
        source._persist()
        published = True
        shutil.rmtree(old)
        store.prune_unreferenced_layers(source.overlaybd_image, source.directory,
                                       store.source_for(source.environment_id))
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if not published and source.snapshot != target and target.exists():
            shutil.rmtree(target)


def fork_baseline(manager, baseline_id, ttl, environment_id, memory_profile,
                  verifier_storage, storage):
    from dsec.runtime.lifecycle import SandboxError, _copy_sparse, _fsync_directory
    if not isinstance(baseline_id, str) or not re.fullmatch(r"[0-9a-f]{12}", baseline_id):
        raise ValueError("Invalid baseline_id")
    if not math.isfinite(ttl) or ttl <= 0:
        raise ValueError("TTL must be finite and positive")
    source = manager.sandboxes.get(baseline_id)
    if source is None:
        raise SandboxError("Unknown baseline")
    started = time.monotonic()
    with source.lock:
        entered = time.monotonic()
        source._check()
        if (not source.baseline_sealed or source.state != "PAUSED" or not source.snapshot or
                getattr(source, 'baseline_closing', False)):
            raise SandboxError("Baseline must be sealed and PAUSED")
        expected = (environment_id, memory_profile, verifier_storage, storage)
        if expected != (source.environment_id, source.memory_profile,
                        source.verifier_storage, source.storage):
            raise SandboxError("Baseline environment/profile mismatch")
        if source.environment_catalog_sha256 is not None:
            spec = manager.microvm_environment_catalog.resolve(environment_id, storage)
            if (spec['environment_sha256'] != source.environment_manifest_sha256 or
                    tuple(x['file'] for x in spec['layers']) != source.layer_disks):
                raise SandboxError("Baseline environment source changed")
        if not source.baseline_verified:
            # One full verification after manager restart, not per branch.
            _validate_snapshot(source)
            source.baseline_identity = _file_identities(source)
            source.baseline_verified = True
            source._persist()
        elif _file_identities(source) != source.baseline_identity:
            raise SandboxError('Prepared artifact identity changed')
        validated = time.monotonic()
        if source.overlaybd_store:
            # Fixed source path for paused snapshot load; it is never run by a
            # guest. Every branch PATCHes to its private device before resume.
            if source.overlaybd_device_id is None:
                store = source.overlaybd_store
                source.overlaybd_device_id, source.overlaybd_runtime = store.create(
                    source.snapshot/'disk-image.json', source.directory, source.disk)
                source.overlaybd_daemon_socket_identity = store.socket_identity()
                source._persist()
            elif not source._overlaybd_service_matches():
                raise SandboxError('Prepared backing service identity changed')
        elif not source.disk.exists():
            _copy_sparse(source.snapshot/'disk.ext4', source.disk)
        anchor_ready = time.monotonic()
        source.fork_readers += 1
        snapshot = source.snapshot
        origin = {'baseline_id': source.id, 'generation': source.generation,
                  'environment_manifest_sha256': source.environment_manifest_sha256}
    # The source lock is released before network, private upper and VMM work.
    child = None
    try:
        child = manager._create_cold(ttl, environment_id, memory_profile,
                                     verifier_storage, storage=storage, prepare_only=True)
        with child.lock:
            try:
                child.fork_origin = origin
                child._persist()
                if source.overlaybd_store:
                    store = source.overlaybd_store
                    image = json.loads((snapshot/'disk-image.json').read_text())
                    layers = store.disk_layers(snapshot/'disk-image.json', source.directory,
                                              store.source_for(environment_id))
                    if any(not store.shared_layers.owns(p) for p in layers):
                        raise SandboxError('Baseline must publish shared checkpoint objects before fork')
                    store.shared_layers.hold(child.id, layers)
                    child.fork_origin['disk_layer_clone_methods'] = {p.name:'shared-lower' for p in layers}
                    image['upper'] = {}
                    child.overlaybd_image = child.directory/'fork-image.json'
                    child.overlaybd_image.write_text(json.dumps(image))
                    child.overlaybd_image.chmod(0o660)
                    with child.overlaybd_image.open('rb') as stream:
                        os.fsync(stream.fileno())
                    _fsync_directory(child.directory)
                    child.overlaybd_device_id, child.overlaybd_runtime = store.create(
                        child.overlaybd_image, child.directory, child.disk)
                    child.overlaybd_daemon_socket_identity = store.socket_identity()
                else:
                    _copy_sparse(snapshot/'disk.ext4', child.disk)
                    child.disk.chmod(0o600)
                if source.work_disk:
                    _copy_sparse(snapshot/'work.ext4', child.work_disk)
                child._persist()
                prepared = time.monotonic()
                child.vm.restore_fork(snapshot/'state', snapshot/'memory', child.disk,
                                      work=child.work_disk,
                                      tap='tap0' if child.network_mode == 'netns' else None)
                restored = time.monotonic()
                result = child.vm.execute(identity_command(child.id))
                if result['exit_code'] != 0 or result.get('timed_out'):
                    raise SandboxError("Fork identity/entropy initialization failed: " + result['output'])
                child._event('RUNNING')
                child._touch()
                child.last_create_phases = {
                    'baseline_lock_wait': entered-started,
                    'baseline_validate': validated-entered,
                    'baseline_anchor_prepare': anchor_ready-validated,
                    'fork_disk_network_prepare': prepared-anchor_ready,
                    'fork_vm_restore': restored-prepared,
                    'fork_identity': time.monotonic()-restored,
                    'fork_total': time.monotonic()-started}
                child._persist()
                return child
            except Exception:
                child._stop('fork_failed_cleanup')
                raise
    finally:
        with source.lock:
            source.fork_readers -= 1
            source.fork_condition.notify_all()
