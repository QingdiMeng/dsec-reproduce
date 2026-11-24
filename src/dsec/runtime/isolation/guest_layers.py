"""Trusted guest EROFS/DAX composition; also copied standalone into guests.

All mounts stay inside the VM. No host mount or FUSE daemon is involved. The
administrator-published manifest fixes layer order; the last entry is topmost.
An unsupported pmem/DAX kernel fails boot, never silently buffers the image.
"""
import ctypes
import json
from pathlib import Path
import subprocess
import time

ROOT = Path('/run/dsec-layers')
PRIVATE = Path('/var/lib/dsec-layers')
if __package__:
    from dsec.contracts.layers import PROBE, PROBE_DATA, manifest
else:
    from layer_contract import PROBE, PROBE_DATA, manifest

STATX_ATTR_DAX = 0x200000


def dax_file(path):
    # Linux statx has a fixed 256-byte ABI. Read the defined attributes fields,
    # not a libc/platform-dependent struct stat layout.
    import os
    buffer=ctypes.create_string_buffer(256)
    libc=ctypes.CDLL(None,use_errno=True)
    function=libc.statx
    function.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_uint,ctypes.c_void_p]
    function.restype=ctypes.c_int
    if function(-100,os.fsencode(path),0,0x7ff,buffer):
        raise OSError(ctypes.get_errno(),'statx DAX probe failed')
    data=buffer.raw
    attributes=int.from_bytes(data[8:16],'little')
    supported=int.from_bytes(data[56:64],'little')
    return bool(attributes & supported & STATX_ATTR_DAX)


def mount_entry(target, mounts=None):
    lines=mounts if mounts is not None else Path('/proc/mounts').read_text().splitlines()
    for line in lines:
        fields=line.split()
        if len(fields)>=4 and fields[1]==str(target):return fields
    raise RuntimeError('Required guest mount is missing: '+str(target))


def attest(value, *, check_dax=dax_file):
    value=manifest(value)
    proofs=[]
    for index,layer in enumerate(value['layers']):
        target=ROOT/str(index);fields=mount_entry(target)
        if (fields[0]!=f'/dev/pmem{index}' or fields[2]!='erofs'
                or not {'ro','dax=always'}<=set(fields[3].split(','))):
            raise RuntimeError('EROFS pmem/DAX mount not enforced')
        probe=target/PROBE
        if probe.read_bytes()!=PROBE_DATA or not check_dax(probe):
            raise RuntimeError('EROFS file does not have active DAX')
        proofs.append(dict(layer,dax=True))
    merged=PRIVATE/'merged';fields=mount_entry(merged)
    if fields[2]!='overlay' or 'rw' not in fields[3].split(','):
        raise RuntimeError('Private guest OverlayFS is not active')
    options=set(fields[3].split(','))
    lower=':'.join(str(ROOT/str(i)) for i in reversed(range(len(value['layers']))))
    if not {f'lowerdir={lower}',f'upperdir={PRIVATE}/upper',f'workdir={PRIVATE}/work'}<=options:
        raise RuntimeError('Shared lower/private upper identity changed')
    if mount_entry(PRIVATE)[2]!='ext4':
        raise RuntimeError('Writable upper must reside on private ext4')
    return dict(readonly_layers=proofs,shared_mounts=value['mounts'])


def prepare(value):
    value=manifest(value)
    ROOT.mkdir(parents=True,exist_ok=False)
    PRIVATE.mkdir(parents=True,exist_ok=True)
    def run(*args):subprocess.run(args,check=True,timeout=30)
    # Self-bind the private root so both its mount identity and ext4 type can be
    # checked. It remains on the already-private writable VM root disk.
    run('mount','--bind',str(PRIVATE),str(PRIVATE))
    for index,layer in enumerate(value['layers']):
        target=ROOT/str(index);target.mkdir()
        device=f'/dev/pmem{index}'
        size=Path('/sys/class/block')/f'pmem{index}'/'size'
        deadline=time.monotonic()+10
        while not size.is_file() or not Path(device).exists():
            if time.monotonic()>=deadline:raise TimeoutError('Required guest pmem device did not appear')
            time.sleep(.02)
        actual=int(size.read_text())*512
        if actual!=layer['bytes']:raise RuntimeError('Guest pmem capacity mismatch')
        run('mount','-t','erofs','-o','ro,dax=always',device,str(target))
    for name in ('upper','work','merged'):(PRIVATE/name).mkdir(exist_ok=False)
    lower=':'.join(str(ROOT/str(i)) for i in reversed(range(len(value['layers']))))
    run('mount','-t','overlay','overlay','-o',
        f'lowerdir={lower},upperdir={PRIVATE}/upper,workdir={PRIVATE}/work',str(PRIVATE/'merged'))
    # Only named directories are exposed to the unprivileged inner container;
    # all mount operations finish before its supervisor publishes readiness.
    for target in value['mounts']:
        source=PRIVATE/'merged'/target.lstrip('/')
        if source.is_symlink() or source.resolve()!=source or not source.is_dir():
            raise ValueError('Shared mount source must be a real directory')
    return attest(value)


if __name__=='__main__':
    import sys
    print(json.dumps(prepare(json.loads(Path(sys.argv[1]).read_text()))))
