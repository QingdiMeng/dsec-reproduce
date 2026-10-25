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
          inner_memory_mb=512, inner_cpus=1, network=None):
    if mode not in ('fncall','container') or not 1024 <= disk_mb <= 8192:
        raise ValueError('Unsupported guest mode/size')
    if network is not None:
        from dsec.runtime.isolation.network_policy import policy
        network=policy(network)
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
        if network is not None:
            shutil.copy2(source/'src/dsec/runtime/isolation/guest_network.py', trusted/'guest_network.py')
        subprocess.run(['cc','-static','-DDSEC_NATIVE_STANDALONE','-O2','-Wall','-Wextra','-Werror',
            '-o',str(trusted/'native-agent'),str(source/'guest_native.c')],check=True)
        # The task image also needs Python for the bounded command helper.
        actual = subprocess.check_output(['docker','image','inspect',task_image,'--format','{{.Id}}'],text=True).strip()
        if actual != task_image or not task_image.startswith('sha256:') or len(task_image) != 71:
            raise ValueError('Exact already-local task image ID required')
        with (trusted/'task.tar').open('wb') as stream:
            subprocess.run(['docker','save',task_image],stdout=stream,check=True)
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
        config=dict(mode=mode,image=inner_image,memory_mb=inner_memory_mb,cpus=inner_cpus)
        if network is not None:config['network']=network
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
        bytes=output.stat().st_size,network=network or False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('base','docker-tools','task-image','source','out','mode'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--disk-mb',type=int,default=2048)
    parser.add_argument('--network-policy',type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.base,args.docker_tools,args.task_image,args.source,args.out,
                           mode=args.mode,disk_mb=args.disk_mb,
                           network=json.loads(args.network_policy.read_text()) if args.network_policy else None)))


if __name__ == '__main__':
    main()
