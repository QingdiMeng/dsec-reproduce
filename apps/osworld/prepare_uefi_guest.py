"""Install the DSec guest service into an offline derivative of OSWorld Ubuntu."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import shlex
import socket
import subprocess
import tarfile
import tempfile
import time

import requests


SERVICE = '''[Unit]
Description=DSec FullVM guest agent
After=local-fs.target systemd-modules-load.service

[Service]
Type=simple
Environment=DSEC_BOOTSTRAP_READY=1
ExecStartPre=/usr/bin/install -d -o 1000 -g 1000 -m 0700 /work /run/dsec-native
ExecStartPre=-/sbin/modprobe virtio_console
ExecStartPre=-/sbin/modprobe vmw_vsock_virtio_transport
ExecStart=/usr/bin/python3 -B /opt/dsec/docker_guest.py serve

[Install]
WantedBy=multi-user.target
'''

INSTALL = '''set -eu
test "$(id -u user)" = 1000
test "$(id -g user)" = 1000
test ! -e /opt/dsec
test ! -e /work
temp="$(mktemp -d)"
trap 'rm -rf -- "$temp"' EXIT
tar -xf /home/user/.dsec-guest-stage.tar -C "$temp"
install -d -m 0755 /opt/dsec
install -m 0644 "$temp/docker_guest.py" "$temp/guest_auth.py" "$temp/execution_contract.py" "$temp/config.json" /opt/dsec/
install -m 0755 "$temp/native-agent" /opt/dsec/native-agent
install -m 0644 "$temp/dsec-guest.service" /etc/systemd/system/dsec-guest.service
systemctl enable dsec-guest.service
rm -f -- /home/user/.dsec-guest-stage.tar
sync
'''


def digest(path):
    with path.open('rb') as file:
        return hashlib.file_digest(file,'sha256').hexdigest()


def bundle(source, work):
    agent = work/'native-agent'
    subprocess.run(['cc','-static','-DDSEC_NATIVE_STANDALONE','-DDSEC_MAX_TIMEOUT_MS=30000',
        '-O2','-Wall','-Wextra','-Werror','-o',str(agent),str(source/'guest_native.c')],
        check=True, timeout=120)
    files = {'native-agent':agent.read_bytes(), 'dsec-guest.service':SERVICE.encode(),
             'config.json':b'{"mode":"fullvm","command_timeout_ms":30000}\n'}
    modules = ['runtime/backends/docker_guest.py','runtime/isolation/guest_auth.py','contracts/execution.py']
    for module in modules:
        target = 'execution_contract.py' if module.endswith('/execution.py') else Path(module).name
        files[target] = (source/'src/dsec'/module).read_bytes()
    archive = work/'agent.tar'
    with tarfile.open(archive,'w') as out:
        for name,data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            out.addfile(info,io.BytesIO(data))
    return archive


def prepare(base, expected, source, code, variables, output):
    base = base.resolve(strict=True)
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    if base.stat().st_mode & 0o222 or digest(base) != expected:
        raise ValueError('Official base must be read-only with the expected SHA-256')
    if shutil.disk_usage(output.parent).free < 8*2**30:
        raise RuntimeError('Need 8 GiB free for the guest service preparation')
    with tempfile.TemporaryDirectory(prefix='osworld-prep-',dir=output.parent) as temp:
        work = Path(temp)
        overlay, nvram = work/'agent.qcow2',work/'vars.fd'
        archive = bundle(source,work)
        subprocess.run(['qemu-img','create','-q','-f','qcow2','-F','qcow2','-b',str(base),str(overlay)],
                       check=True, timeout=20)
        shutil.copyfile(variables,nvram)
        with socket.socket() as reserve:
            reserve.bind(('127.0.0.1',0))
            port = reserve.getsockname()[1]
        address = f'http://127.0.0.1:{port}'
        args = ['qemu-system-x86_64','-nodefaults','-no-user-config','-machine','q35,accel=kvm',
                '-cpu','host','-m','4096','-smp','4','-display','none',
                '-drive',f'if=pflash,format=raw,readonly=on,file={code}',
                '-drive',f'if=pflash,format=raw,file={nvram}',
                '-drive',f'if=none,id=root,format=qcow2,file={overlay}',
                '-device','virtio-blk-pci,drive=root,bootindex=0','-device','VGA',
                '-device','qemu-xhci','-device','usb-tablet','-device','usb-kbd',
                '-netdev',f'user,id=prep,restrict=on,hostfwd=tcp:127.0.0.1:{port}-:5000',
                '-device','virtio-net-pci,netdev=prep',
                '-sandbox','on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny']
        with output.with_suffix('.log').open('xb') as log, requests.Session() as session:
            session.trust_env = False
            proc = subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=log,stderr=log)
            try:
                deadline = time.monotonic()+100
                while time.monotonic() < deadline and proc.poll() is None:
                    try:
                        reply = session.get(address+'/screenshot',timeout=2)
                        if reply.ok and reply.content.startswith(b'\x89PNG\r\n\x1a\n'):
                            break
                    except requests.RequestException:
                        pass
                    time.sleep(.5)
                else:
                    raise RuntimeError('Official guest service did not become ready')
                with archive.open('rb') as file:
                    reply = session.post(address+'/setup/upload',
                        data={'file_path':'/home/user/.dsec-guest-stage.tar'},
                        files={'file_data':('agent.tar',file)},timeout=30)
                reply.raise_for_status()
                # The unmodified benchmark VM ships this guest-local sudo password.
                def execute(command):
                    reply = session.post(address+'/execute',json={'command':command,'shell':True},
                                         timeout=90)
                    reply.raise_for_status()
                    value = reply.json()
                    if value.get('returncode') != 0:
                        raise RuntimeError('Guest preparation failed: '+str(value)[:1200])
                execute("printf '%s\\n' password | sudo -S /bin/sh -c "+shlex.quote(INSTALL))
                execute("printf '%s\\n' password | sudo -S systemd-run --on-active=2s /sbin/poweroff")
                if proc.wait(timeout=65) != 0:
                    raise RuntimeError('Preparation guest did not shut down cleanly')
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=8)
        overlay.chmod(0o444)
        pin = digest(overlay)
        os.link(overlay,output)
    return {'path':str(output),'sha256':pin,'official_base_sha256':expected}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('base','source','code','variables','out'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--base-sha256',required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.base,args.base_sha256,args.source,args.code,args.variables,args.out)))


if __name__ == '__main__':
    main()
