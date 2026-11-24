"""Prepare a QEMU guest from three pinned, already-local images.

Never runs a host dind daemon. Temporary host containers are only exported while
stopped. Inner Docker runs after boot, entirely inside its private QEMU guest.
Requires Linux mkfs.ext4, a static C compiler and a trusted Python base image.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tarfile

from build_smoke_guest import export_image


def build(base, docker_tools, task_image, source, output, *, mode, disk_mb=2048,
          inner_memory_mb=512, inner_cpus=1, network=None,storage_driver='vfs',
          readonly_layers=(), shared_mounts=(),task_archive=None,task_archive_sha256=None,command_timeout_ms=30000):
    from dsec.contracts.execution import command_timeout
    command_timeout(command_timeout_ms)
    if mode not in ('fncall','container') or not 1024 <= disk_mb <= 8192:
        raise ValueError('Unsupported guest mode/size')
    if storage_driver not in ('vfs','overlay2'):raise ValueError('Unsupported inner Docker storage driver')
    if network is not None:
        from dsec.runtime.isolation.network_policy import policy
        network=policy(network)
    if (task_image is None)==(task_archive is None):raise ValueError('Select one pinned task image or archive')
    if task_archive is not None:
        import re
        task_archive=Path(task_archive)
        if (not task_archive.is_absolute() or task_archive.is_symlink() or task_archive.resolve(strict=True)!=task_archive
                or not task_archive.is_file() or not isinstance(task_archive_sha256,str)
                or not re.fullmatch('[a-f0-9]{64}',task_archive_sha256)):
            raise ValueError('Canonical task archive and SHA-256 required')
        with task_archive.open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
        if actual!=task_archive_sha256:raise ValueError('Task archive identity mismatch')
    elif task_archive_sha256 is not None:raise ValueError('Archive pin requires an archive')
    layers=None
    if readonly_layers:
        from dsec.runtime.backends.qemu import QemuReadOnlyLayer
        from dsec.storage.erofs import guest_manifest
        for layer in readonly_layers:QemuReadOnlyLayer(**layer).verify()
        layers=guest_manifest(readonly_layers,list(shared_mounts))
    elif shared_mounts:raise ValueError('Shared mounts require EROFS/DAX layers')
    source = Path(source).resolve(strict=True)
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.dsec-qemu-build-',dir=output.parent) as temporary:
        work = Path(temporary); root = work/'root'; root.mkdir()
        export_image(base, root, work/'root.tar')
        tools = work/'tools'; tools.mkdir()
        export_image(docker_tools, tools, work/'tools.tar')
        for name in ('docker','dockerd','containerd','containerd-shim-runc-v2','ctr','runc','docker-init'):
            binary = tools/'usr/local/bin'/name
            if not binary.is_file():
                raise ValueError('Docker tools image lacks '+name)
            shutil.copy2(binary, root/'usr/local/bin'/name)
        # Validate required tools in the prepared root, without chrooting on host.
        for name in ('bin/sh','bin/mount','usr/local/bin/python3'):
            if not (root/name).exists():
                raise ValueError('Trusted guest base lacks '+name)
        trusted = root/'opt/dsec'; trusted.mkdir(parents=True)
        shutil.copy2(source/'src/dsec/runtime/backends/docker_guest.py', trusted/'docker_guest.py')
        shutil.copy2(source/'src/dsec/runtime/isolation/guest_auth.py', trusted/'guest_auth.py')
        shutil.copy2(source/'src/dsec/contracts/execution.py', trusted/'execution_contract.py')
        if layers is not None:
            shutil.copy2(source/'src/dsec/runtime/isolation/guest_layers.py',trusted/'guest_layers.py')
            shutil.copy2(source/'src/dsec/contracts/layers.py',trusted/'layer_contract.py')
            (trusted/'layers.json').write_text(json.dumps(layers))
        if network is not None:
            shutil.copy2(source/'src/dsec/runtime/isolation/guest_network.py', trusted/'guest_network.py')
        subprocess.run(['cc','-static','-DDSEC_NATIVE_STANDALONE',f'-DDSEC_MAX_TIMEOUT_MS={command_timeout_ms}','-O2','-Wall','-Wextra','-Werror',
            '-o',str(trusted/'native-agent'),str(source/'guest_native.c')],check=True)
        # The task image also needs Python for the bounded command helper.
        if task_archive is None:
            actual = subprocess.check_output(['docker','image','inspect',task_image,'--format','{{.Id}}'],text=True).strip()
            if actual != task_image or not task_image.startswith('sha256:') or len(task_image) != 71:
                raise ValueError('Exact already-local task image ID required')
            with (trusted/'task.tar').open('wb') as stream:
                subprocess.run(['docker','save',task_image],stdout=stream,check=True)
        else:
            shutil.copyfile(task_archive,trusted/'task.tar')
            with (trusted/'task.tar').open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
            if actual!=task_archive_sha256:raise ValueError('Task archive identity mismatch')
        # Containerd-backed Docker may expose a manifest ID on the host while
        # classic Docker identifies the loaded image by its configuration blob.
        # Derive that exact identity from the pinned exported archive, not a tag.
        with tarfile.open(trusted/'task.tar') as archive:
            manifest = json.load(archive.extractfile('manifest.json'))
            if len(manifest) != 1:
                raise ValueError('Exactly one exported task image required')
            stream = archive.extractfile(manifest[0]['Config'])
            config_blob = stream.read(8*1024**2+1)
            if len(config_blob) > 8*1024**2:
                raise ValueError('Oversized task image config')
            inner_image = 'sha256:'+hashlib.sha256(config_blob).hexdigest()
        config=dict(mode=mode,image=inner_image,memory_mb=inner_memory_mb,cpus=inner_cpus,command_timeout_ms=command_timeout_ms)
        if network is not None:config['network']=network
        if layers is not None:config['layers']=layers
        (trusted/'config.json').write_text(json.dumps(config))
        for name in ('proc','sys','dev','tmp','run','var/lib/dsec-work'):
            (root/name).mkdir(parents=True,exist_ok=True)
        (root/'tmp').chmod(0o1777)
        init = root/'dsec-init'
        init.write_text('''#!/bin/sh
set -eux
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
if ! mountpoint -q /dev; then mount -t devtmpfs devtmpfs /dev; fi
mkdir -p /dev/pts /dev/shm /run/dsec-docker /run/dsec-native /sys/fs/cgroup
mount -t devpts devpts /dev/pts
mount -t tmpfs -o mode=1777 tmpfs /dev/shm
mount -t cgroup2 cgroup2 /sys/fs/cgroup
chown 1000:1000 /var/lib/dsec-work /run/dsec-native
dockerd --host=unix:///run/dsec-docker/docker.sock --data-root=/var/lib/dsec-docker --exec-root=/run/dsec-docker/exec --pidfile=/run/dsec-docker/pid --bridge=none --iptables=false --ip6tables=false --ip-masq=false --userland-proxy=false --storage-driver=vfs --exec-opt native.cgroupdriver=cgroupfs &
daemon=$!
i=0
until docker --host=unix:///run/dsec-docker/docker.sock info >/dev/null 2>&1; do
  kill -0 "$daemon"
  i=$((i+1)); [ "$i" -lt 100 ] || exit 1
  sleep 0.1
done
docker --host=unix:///run/dsec-docker/docker.sock load -i /opt/dsec/task.tar
exec python3 -B /opt/dsec/docker_guest.py serve
''')
        init.chmod(0o755)
        if storage_driver!='vfs':
            init.write_text(init.read_text().replace('--storage-driver=vfs','--storage-driver='+storage_driver,1))
        if layers is not None:
            init.write_text(init.read_text().replace('dockerd --host=',
                'python3 -B /opt/dsec/guest_layers.py /opt/dsec/layers.json\ndockerd --host=',1))
        if network is not None:
            init.write_text(init.read_text().replace('dockerd --host=',
                'python3 -B /opt/dsec/guest_network.py /opt/dsec/config.json\ndockerd --host=',1))
        disk = work/'guest.ext4'
        with disk.open('wb') as stream:
            stream.truncate(disk_mb*1024**2)
        subprocess.run(['mkfs.ext4','-q','-F','-d',str(root),str(disk)],check=True)
        disk.chmod(0o444); os.link(disk,output)
    with output.open('rb') as stream:
        digest = hashlib.file_digest(stream,'sha256').hexdigest()
    return dict(template=str(output),sha256=digest,mode=mode,base_image=base,
        docker_tools_image=docker_tools,task_image=task_image,inner_image=inner_image,
        task_archive=str(task_archive) if task_archive else None,task_archive_sha256=task_archive_sha256,
        bytes=output.stat().st_size,network=network or False,storage_driver=storage_driver,command_timeout_ms=command_timeout_ms,
        readonly_layers=list(readonly_layers),shared_mounts=list(shared_mounts))


def build_fullvm(base, source, output, *, disk_mb=512, command_timeout_ms=30000, desktop=False):
    """Build a direct-boot Linux guest; Docker is only the offline rootfs exporter."""
    from dsec.contracts.execution import command_timeout
    command_timeout(command_timeout_ms)
    if not 256 <= disk_mb <= 8192:
        raise ValueError('FullVM template must be 256..8192 MiB')
    source = Path(source).resolve(strict=True)
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.dsec-fullvm-build-', dir=output.parent) as temporary:
        work = Path(temporary)
        root = work/'root'
        root.mkdir()
        root.chmod(0o755)  # mkfs copies root mode even under a private build umask.
        export_image(base, root, work/'root.tar', preserve_permissions=True)
        for name in ('bin/sh','bin/mount','usr/bin/setpriv'):
            if not (root/name).exists():
                raise ValueError('FullVM base lacks '+name)
        if not any((root/name).exists() for name in ('usr/local/bin/python3','usr/bin/python3')):
            raise ValueError('FullVM base lacks Python 3')
        if desktop and not (root/'opt/dsec-desktop/start').is_file():
            raise ValueError('Desktop base requires the trusted /opt/dsec-desktop/start hook')
        trusted = root/'opt/dsec'
        trusted.mkdir(parents=True)
        trusted.chmod(0o755)
        for module, target in (
            ('runtime/backends/docker_guest.py','docker_guest.py'),
            ('runtime/isolation/guest_auth.py','guest_auth.py'),
            ('contracts/execution.py','execution_contract.py')):
            shutil.copy2(source/'src/dsec'/module, trusted/target)
            (trusted/target).chmod(0o644)
        (trusted/'config.json').write_text(json.dumps(dict(mode='fullvm',
                                                          command_timeout_ms=command_timeout_ms)))
        subprocess.run(['cc','-static','-DDSEC_NATIVE_STANDALONE',
            f'-DDSEC_MAX_TIMEOUT_MS={command_timeout_ms}','-O2','-Wall','-Wextra','-Werror',
            '-o',str(trusted/'native-agent'),str(source/'guest_native.c')],check=True)
        (trusted/'native-agent').chmod(0o755)
        for name in ('proc','sys','dev','tmp','run','work'):
            (root/name).mkdir(exist_ok=True)
        init = root/'dsec-init'
        init.write_text("""#!/bin/sh
set -eu
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# The unprivileged offline exporter does not preserve guest UID/GID ownership.
find / -xdev -exec chown -h 0:0 '{}' +
chmod 755 / /run /opt /opt/dsec
mount -t proc proc /proc
mount -t sysfs sysfs /sys
if ! mountpoint -q /dev; then mount -t devtmpfs devtmpfs /dev; fi
mkdir -p /dev/pts /dev/shm /run/dsec-native
mount -t devpts devpts /dev/pts
mount -t tmpfs -o mode=1777 tmpfs /dev/shm
chmod 1777 /tmp
chmod 700 /work /run/dsec-native
chown 1000:1000 /work /run/dsec-native
exec python3 -B /opt/dsec/docker_guest.py serve
""")
        if desktop:
            init.write_text(init.read_text().replace('exec python3 -B',
                '/opt/dsec-desktop/start\nexec python3 -B',1))
        init.chmod(0o755)
        disk = work/'guest.ext4'
        with disk.open('wb') as stream:
            stream.truncate(disk_mb*1024**2)
        subprocess.run(['mkfs.ext4','-q','-F','-d',str(root),str(disk)],check=True)
        disk.chmod(0o444)
        os.link(disk,output)
    with output.open('rb') as stream:
        digest = hashlib.file_digest(stream,'sha256').hexdigest()
    return dict(template=str(output),sha256=digest,mode='fullvm',base_image=base,
                bytes=output.stat().st_size,command_timeout_ms=command_timeout_ms,
                **({'display':'virtio-2d'} if desktop else {}))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('base','source','out','mode'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--desktop',action='store_true',help='Enable the trusted FullVM desktop startup hook')
    parser.add_argument('--docker-tools')
    task=parser.add_mutually_exclusive_group()
    task.add_argument('--task-image')
    task.add_argument('--task-archive',type=Path)
    parser.add_argument('--task-archive-sha256')
    parser.add_argument('--disk-mb',type=int)
    parser.add_argument('--command-timeout-ms',type=int,default=30000)
    parser.add_argument('--storage-driver',choices=('vfs','overlay2'),default='vfs')
    parser.add_argument('--network-policy',type=Path)
    parser.add_argument('--readonly-layers',type=Path,help='JSON list of pinned EROFS/DAX layer artifacts, bottom first')
    parser.add_argument('--shared-mount',action='append',default=[],help='Composed directory exposed inside the task')
    args = parser.parse_args()
    if args.mode == 'fullvm':
        if any((args.docker_tools,args.task_image,args.task_archive,args.task_archive_sha256,
                args.network_policy,args.readonly_layers,args.shared_mount,args.storage_driver!='vfs')):
            parser.error('FullVM uses a standalone local guest, without inner Docker options')
        print(json.dumps(build_fullvm(args.base,args.source,args.out,
            disk_mb=512 if args.disk_mb is None else args.disk_mb,command_timeout_ms=args.command_timeout_ms,desktop=args.desktop)))
        return
    if args.desktop:
        parser.error('--desktop requires --mode fullvm')
    if not args.docker_tools or not (args.task_image or args.task_archive):
        parser.error('Container and FnCall require --docker-tools and a task image/archive')
    print(json.dumps(build(args.base,args.docker_tools,args.task_image,args.source,args.out,
                           mode=args.mode,disk_mb=2048 if args.disk_mb is None else args.disk_mb,storage_driver=args.storage_driver,
                           network=json.loads(args.network_policy.read_text()) if args.network_policy else None,
                           readonly_layers=json.loads(args.readonly_layers.read_text()) if args.readonly_layers else (),
                           shared_mounts=args.shared_mount,task_archive=args.task_archive,task_archive_sha256=args.task_archive_sha256,command_timeout_ms=args.command_timeout_ms)))


if __name__ == '__main__':
    main()
