#!/usr/bin/python3
"""Standalone root broker: generated per-VM AppArmor policy, never VMM launch.

Only a fixed runtime root, binaries and namespace are accepted. A claimed
launcher PID must be dead before policy removal. Retirement tombstones fence
delayed claims; root and the configured Edge UID remain trusted principals.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import stat
import subprocess
import sys
import tempfile

CONFIG = Path('/etc/dsec-qemu-profile.json')
STATE = Path('/var/lib/dsec-qemu-profile')


def safe_path(value):
    path=Path(value)
    if (not path.is_absolute() or not re.fullmatch('/[A-Za-z0-9_./@-]+',str(path))
            or '..' in path.parts or str(path)!=value):
        raise ValueError('Unsafe policy path')
    return path


def validate(cfg):
    required={'uid','prefix','runtime_root','binary','python','launcher','pins'}
    if (not required<=set(cfg)<=required|{'kernel','tun_root','layer_root','uefi_boots'}
            or not ('kernel' in cfg or cfg.get('uefi_boots'))
            or type(cfg['uid']) is not int or cfg['uid']<=0
            or not re.fullmatch('dsec-[a-z0-9-]{1,40}',cfg['prefix'])):
        raise ValueError('Invalid fixed AppArmor broker configuration')
    pins = {'binary','python','launcher'} | ({'kernel'} if 'kernel' in cfg else set())
    for name in pins|{'runtime_root'}:safe_path(cfg[name])
    if 'tun_root' in cfg:safe_path(cfg['tun_root'])
    if 'layer_root' in cfg:
        root=safe_path(cfg['layer_root']);private=safe_path(cfg['runtime_root'])
        if len(root.parts)<3 or root.is_relative_to(private) or private.is_relative_to(root):
            raise ValueError('Shared publication root must be scoped and disjoint from private state')
    if set(cfg['pins'])!=pins or any(
            not re.fullmatch('[a-f0-9]{64}',value) for value in cfg['pins'].values()):
        raise ValueError('Pinned broker artifacts required')
    boots = cfg.get('uefi_boots',[])
    if not isinstance(boots,list):
        raise ValueError('Expected administrator-approved UEFI boot files')
    for files in boots:
        validate_uefi_files(cfg,files)


def profile_name(cfg,sid):
    if not re.fullmatch('[a-f0-9]{32}',sid):raise ValueError('Invalid sandbox identity')
    return cfg['prefix']+'-'+sid


def checked_layers(cfg,layers,*,check_files=False):
    if not isinstance(layers,list) or len(layers)>8:raise ValueError('Bounded pinned shared layers required')
    paths=set()
    for layer in layers:
        if (not isinstance(layer,dict) or set(layer)!={'path','sha256','bytes'}
                or not isinstance(layer['sha256'],str) or not re.fullmatch('[a-f0-9]{64}',layer['sha256'])
                or type(layer['bytes']) is not int or not 128*2**20<=layer['bytes']<=32*2**30
                or layer['bytes']%(128*2**20)):
            raise ValueError('Invalid shared layer pin')
        path=safe_path(layer['path'])
        if ('layer_root' not in cfg or path==Path(cfg['layer_root'])
                or not path.is_relative_to(cfg['layer_root']) or str(path) in paths):
            raise ValueError('Shared layer outside administrator-scoped publication root')
        paths.add(str(path))
        if check_files:
            info=path.lstat()
            if (path.resolve(strict=True)!=path or not stat.S_ISREG(info.st_mode)
                    or info.st_mode & 0o222 or info.st_size!=layer['bytes']):
                raise ValueError('Shared layer identity/permissions changed')
    return layers


def validate_uefi_files(cfg,files,*,check_files=False):
    if not isinstance(files,dict) or not {'code','template'}<=files.keys()<={'code','template','base'}:
        raise ValueError('Expected pinned UEFI firmware and QCOW2 files')
    paths=set()
    for pin in files.values():
        if (not isinstance(pin,dict) or set(pin)!={'path','sha256'}
                or not isinstance(pin['sha256'],str) or not re.fullmatch('[a-f0-9]{64}',pin['sha256'])):
            raise ValueError('Invalid UEFI file pin')
        path=safe_path(pin['path'])
        if path.is_relative_to(cfg['runtime_root']) or path in paths:
            raise ValueError('UEFI boot files must be distinct and outside private VM state')
        paths.add(path)
        if check_files:
            info=path.lstat()
            if path.resolve(strict=True)!=path or not stat.S_ISREG(info.st_mode) or info.st_mode&0o222:
                raise ValueError('UEFI boot file must be canonical, regular and read-only')
    return files


def checked_uefi(cfg,files,*,check_files=False):
    if files is None:
        if 'kernel' not in cfg:
            raise ValueError('Broker is configured for UEFI boot only')
        return None
    if files not in cfg.get('uefi_boots',[]):
        raise ValueError('UEFI boot files are not approved by the administrator')
    return validate_uefi_files(cfg,files,check_files=check_files)


def render_profile(cfg,sid,layers=None,uefi=None):
    validate(cfg);name=profile_name(cfg,sid);directory=str(Path(cfg['runtime_root'])/sid)
    checked_uefi(cfg,uefi)
    if uefi is not None and layers:
        raise ValueError('UEFI profile does not attach pmem')
    tun = ('  "'+cfg['tun_root']+'/'+sid+'/tun" rw,\n') if 'tun_root' in cfg and uefi is None else ''
    shared=''.join('  "'+layer['path']+'" mr,\n' for layer in checked_layers(cfg,layers or []))
    if uefi is None:
        boot_reads=f'  "{cfg["kernel"]}" r,\n'
        disk_rules=f'  "{directory}/root.ext4" rwk,\n'
    else:
        boot_reads=''.join(f'  "{pin["path"]}" rk,\n' for _,pin in sorted(uefi.items()))
        disk_rules=(f'  "{directory}/root.qcow2" rwk,\n'
                    f'  "{directory}/uefi-vars.fd" rwk,\n'
                    f'  "{directory}/display.png" rw,\n')
    # No blanket file/execute/capability rule. A VMM has its own disk and two
    # private listeners; it cannot read the Edge's key/journals or another VM.
    return f'''profile {name} flags=(mediate_deleted) {{
  "{cfg['binary']}" mr,
{boot_reads}  "/usr/lib/**" mr,
  "/lib/**" mr,
  "/lib64/**" mr,
  "/etc/ld.so.cache" r,
  "/etc/localtime" r,
  "/usr/share/qemu/**" r,
  "/usr/share/seabios/**" r,
  "/dev/kvm" rw,
  "/dev/vhost-vsock" rw,
  "/dev/null" rw,
  "/dev/zero" rw,
  "/dev/urandom" r,
  "/proc/" r,
  "/proc/cpuinfo" r,
  "/proc/meminfo" r,
  "/proc/sys/vm/overcommit_memory" r,
  owner /proc/[0-9]*/{{stat,status,statm,auxv,maps,smaps,numa_maps,cgroup,comm}} r,
  owner /proc/[0-9]*/task/[0-9]*/{{stat,status,comm}} r,
  "/sys/devices/system/**" r,
  "/sys/kernel/mm/**" r,
{disk_rules}  "{directory}/console.log" rw,
  "{directory}/vmm.log" w,
  "{directory}/qmp.sock" rw,
  "{directory}/bootstrap.sock" rw,
{tun}{shared}\
  # Address-qualified denies also mask address-independent socket creation.
  # Allow creation and anonymous internal IPC; pathname sockets still require
  # the two exact file rules above. Abstract binds/connects remain ungranted.
  unix (create) type=stream,
  unix (getattr, setattr, getopt, setopt, shutdown) type=stream addr=none,
  unix (send, receive) type=stream addr=none peer=(label={name}, addr=none),
  signal (receive) peer=unconfined,
  signal (send,receive) peer={name},
  ptrace (readby) peer=unconfined,
}}
'''


def save(path,value):
    fd,temporary=tempfile.mkstemp(prefix='.profile-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(value,stream,sort_keys=True);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
        fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:Path(temporary).unlink(missing_ok=True)


def identity(pid):
    proc=Path('/proc')/str(pid)
    return dict(pid=pid,uid=proc.stat().st_uid,
        start_ticks=(proc/'stat').read_text().rsplit(')',1)[1].split()[19],
        boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        exe=str((proc/'exe').resolve(strict=True)),
        argv=(proc/'cmdline').read_bytes().decode().strip('\0').split('\0'))


def prove_dead(saved):
    if saved['boot_id']!=Path('/proc/sys/kernel/random/boot_id').read_text().strip():return
    try:fd=os.pidfd_open(saved['pid'])
    except ProcessLookupError:return
    try:
        if select.select([fd],[],[],0)[0]:return
        # An unreadable/deleted executable is not death. Only stable start
        # identity (PID reuse) or the pidfd death event can discharge a claim.
        try:
            proc=Path('/proc')/str(saved['pid'])
            started=(proc/'stat').read_text().rsplit(')',1)[1].split()[19]
        except FileNotFoundError:
            if select.select([fd],[],[],0)[0]:return
            raise RuntimeError('Claimed identity missing while pidfd remains live')
        if started!=saved['start_ticks']:return
        raise RuntimeError('Claimed launch process is still live; profile retained')
    finally:os.close(fd)


def checked_config():
    if os.geteuid()!=0:raise PermissionError('Administrator broker required')
    for kind in ('mnt','user'):
        if os.readlink('/proc/self/ns/'+kind)!=os.readlink('/proc/1/ns/'+kind):
            raise PermissionError('Host namespace context required')
    for path,directory in ((CONFIG,False),(STATE,True)):
        info=path.lstat()
        if (info.st_uid!=0 or info.st_mode & 0o022 or
                not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
            raise PermissionError('Unsafe administrator broker target')
    cfg=json.loads(CONFIG.read_text());validate(cfg)
    if int(os.environ.get('SUDO_UID',-1))!=cfg['uid']:
        raise PermissionError('Broker caller identity mismatch')
    if Path('/sys/module/apparmor/parameters/enabled').read_text().strip()!='Y':
        raise PermissionError('AppArmor is not enabled')
    if Path('/sys/kernel/security/apparmor/features/network/af_unix').read_text().strip()!='yes':
        raise PermissionError('Fine-grained AppArmor Unix socket mediation required')
    for name,expected in cfg['pins'].items():
        path=safe_path(cfg[name]);info=path.stat()
        if path.resolve(strict=True)!=path or not stat.S_ISREG(info.st_mode):
            raise ValueError('Broker artifact must be canonical and regular')
        with path.open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
        if actual!=expected:raise ValueError('Broker artifact changed: '+name)
    return cfg


def operate(cfg,operation,sid,arguments):
    if operation=='retire' and arguments:
        raise ValueError('Unexpected retire arguments')
    profile=profile_name(cfg,sid)
    record_path=STATE/(sid+'.json');policy_path=STATE/(sid+'.profile')
    old=json.loads(record_path.read_text()) if record_path.exists() else None
    if old and (old.get('state') not in ('PREPARING','PREPARED','CLAIMED','RETIRED') or
                set(old)!={'profile','sha256','state'}|({'process'} if old.get('state')=='CLAIMED' else set())|
                ({'readonly_layers'} if 'readonly_layers' in old else set())|
                ({'uefi'} if 'uefi' in old else set())):
        raise RuntimeError('Corrupt profile ledger; recovery required')
    layers=old.get('readonly_layers',[]) if old else []
    uefi=old.get('uefi') if old else None
    if operation=='prepare':
        if len(arguments)>2 or any(len(argument)>16384 for argument in arguments):
            raise ValueError('Bounded prepare artifacts required')
        requested=checked_layers(cfg,json.loads(arguments[0]) if arguments else [],check_files=True)
        selected=checked_uefi(cfg,json.loads(arguments[1]) if len(arguments)==2 else None,check_files=True)
        if old and (requested!=layers or selected!=uefi):
            raise RuntimeError('Persisted shared layer identity changed')
        layers=requested
        uefi=selected
    checked_layers(cfg,layers)
    if operation=='retire' and old is None and not policy_path.exists() and 'kernel' not in cfg:
        # Prepare may have failed before creating a policy. An empty UEFI-only
        # ledger needs no profile definition, but must fence future claims.
        if any(line.startswith(profile+' (') for line in
               Path('/sys/kernel/security/apparmor/profiles').read_text().splitlines()):
            raise RuntimeError('Unowned policy is already loaded')
        value=dict(profile=profile,sha256=None,state='RETIRED')
        save(record_path,value)
        return value
    if old and old['state']=='RETIRED' and old['sha256'] is None:
        if operation=='retire' and not arguments:
            return old
        raise RuntimeError('Profile already retired; no new launch')
    text=render_profile(cfg,sid,layers,uefi);digest=hashlib.sha256(text.encode()).hexdigest()
    if old and (old['profile']!=profile or old['sha256']!=digest):
        raise RuntimeError('Persisted profile identity changed')
    def commit(state,**fields):
        value=dict(profile=profile,sha256=digest,state=state,**fields)
        if layers:value['readonly_layers']=layers
        if uefi is not None:value['uefi']=uefi
        save(record_path,value);return value
    lines=lambda:Path('/sys/kernel/security/apparmor/profiles').read_text().splitlines()
    loaded=lambda:profile+' (enforce)' in lines()
    present=lambda:any(line.startswith(profile+' (') for line in lines())
    if operation=='prepare':
        if old and old['state'] in ('CLAIMED','RETIRED'):
            raise RuntimeError('Profile already claimed or retired; no new launch')
        if old is None:
            if policy_path.exists():raise RuntimeError('Profile ledger missing; recovery required')
            if present():raise RuntimeError('Unowned policy is already loaded')
            commit('PREPARING')
        if policy_path.exists() and policy_path.read_text()!=text:
            raise RuntimeError('Policy file identity changed')
        policy_path.write_text(text);policy_path.chmod(0o600)
        if not loaded():
            subprocess.run(['/usr/sbin/apparmor_parser','-a','-K',str(policy_path)],check=True,timeout=10,
                           stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        if not loaded():raise RuntimeError('Enforcing policy not attested')
        return commit('PREPARED')
    if operation=='claim':
        if len(arguments)!=2 or not arguments[0].isdecimal() or not arguments[1].isdecimal():
            raise ValueError('Expected a launcher PID and start time')
        if not old or old['state'] not in ('PREPARED','CLAIMED'):
            raise RuntimeError('Launch claim fenced by profile state')
        saved=identity(int(arguments[0]));argv=saved['argv']
        if (saved['uid']!=cfg['uid'] or saved['start_ticks']!=arguments[1]
                or saved['exe']!=cfg['python'] or len(argv)!=6
                or argv[:4]!=[cfg['python'],'-I','-B',cfg['launcher']]
                or argv[4]!=str(Path(cfg['runtime_root'])/sid/'launch.json') or not argv[5].isdecimal()):
            raise ValueError('Launcher identity not attested')
        if old['state']=='CLAIMED' and old['process']!=saved:
            raise RuntimeError('A different process already claimed this profile')
        if not loaded():raise RuntimeError('Profile missing before launch claim')
        return commit('CLAIMED',process=saved)
    if operation=='retire':
        if old and old['state']=='CLAIMED':prove_dead(old['process'])
        if old is None and policy_path.exists():raise RuntimeError('Profile ledger missing; retirement refused')
        # Persist the fence *before* unload; a delayed claim must reject even
        # while parser removal is blocked or the broker crashes.
        result=commit('RETIRED')
        if present():
            if not policy_path.exists() or policy_path.read_text()!=text:
                raise RuntimeError('Profile removal requires its original policy')
            subprocess.run(['/usr/sbin/apparmor_parser','-R','-K',str(policy_path)],check=True,timeout=10,
                           stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        if present():raise RuntimeError('Profile remains loaded')
        return result
    raise ValueError('Only prepare, claim and retire are supported')


def main():
    cfg=checked_config()
    if len(sys.argv)<3:raise ValueError('Expected operation and sandbox identity')
    with (STATE/'broker.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        print(json.dumps(operate(cfg,sys.argv[1],sys.argv[2],sys.argv[3:])))


if __name__=='__main__':main()
