#!/usr/bin/python3
"""Fixed-instance root broker for isolated QEMU TAPs; never launches a VMM.

Only a sandbox identity and an attested gated launcher PID enter this protocol.
TAP descriptors are delivered once to the runtime user's private Unix socket.
"""
import array
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import select
import socket
import stat
import struct
import subprocess
import sys
import tempfile

CONFIG=Path('/etc/dsec-qemu-network.json')
STATE=Path('/var/lib/dsec-qemu-network')
GUEST='169.254.110.2'
GATEWAY='169.254.110.1'
POOL=ipaddress.IPv4Network('10.232.0.0/16')
LOCK_FD=None


def run(*args):
    return subprocess.check_output(args,text=True,stderr=subprocess.PIPE,timeout=15,pass_fds=(() if LOCK_FD is None else (LOCK_FD,)))


def exists(*args):
    return subprocess.run(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15,pass_fds=(() if LOCK_FD is None else (LOCK_FD,))).returncode==0


def root_owned(path,directory=False):
    info=path.lstat()
    if info.st_uid!=0 or info.st_mode & 0o022 or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise PermissionError('Unsafe root-owned broker artifact: '+str(path))


def save(path,value):
    fd,temp=tempfile.mkstemp(prefix='.network-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(value,stream,sort_keys=True);stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
        fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:Path(temp).unlink(missing_ok=True)


def identity(pid):
    proc=Path('/proc')/str(pid)
    return dict(pid=pid,uid=proc.stat().st_uid,start_ticks=(proc/'stat').read_text().rsplit(')',1)[1].split()[19],
                boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                exe=str((proc/'exe').resolve(strict=True)),argv=(proc/'cmdline').read_bytes().decode().strip('\0').split('\0'))


def prove_dead(saved):
    if saved['boot_id']!=Path('/proc/sys/kernel/random/boot_id').read_text().strip():return
    try:fd=os.pidfd_open(saved['pid'])
    except ProcessLookupError:return
    try:
        if select.select([fd],[],[],0)[0]:return
        try:started=(Path('/proc')/str(saved['pid'])/'stat').read_text().rsplit(')',1)[1].split()[19]
        except FileNotFoundError:
            if select.select([fd],[],[],0)[0]:return
            raise RuntimeError('Process details missing while pidfd remains live')
        if started!=saved['start_ticks']:return
        raise RuntimeError('Claimed QEMU launch process still live; network retained')
    finally:os.close(fd)


def checked_config():
    if os.geteuid()!=0:raise PermissionError('Administrator broker required')
    for kind in ('net','mnt','user'):
        if os.readlink('/proc/self/ns/'+kind)!=os.readlink('/proc/1/ns/'+kind):raise PermissionError('Host namespace required')
    root_owned(CONFIG);root_owned(STATE,True)
    cfg=json.loads(CONFIG.read_text())
    if (set(cfg)!={'uid','prefix','runtime_root','uplink','slot_offset','max_slots','policy','pins','python','launcher','object'} or
            type(cfg['uid']) is not int or cfg['uid']<=0 or int(os.environ.get('SUDO_UID',-1))!=cfg['uid'] or
            not re.fullmatch('dsec-[a-z0-9-]{1,40}',cfg['prefix']) or
            not re.fullmatch('[a-zA-Z0-9_.-]{1,15}',cfg['uplink']) or
            type(cfg['slot_offset']) is not int or type(cfg['max_slots']) is not int or
            not 0<=cfg['slot_offset']<32768 or not 1<=cfg['max_slots']<=32768-cfg['slot_offset']):
        raise ValueError('Invalid fixed network broker configuration')
    if set(cfg['pins'])!={'python','launcher','object'}:raise ValueError('Pinned broker inputs required')
    for name in ('runtime_root','python','launcher','object'):
        path=Path(cfg[name])
        if not re.fullmatch('/[A-Za-z0-9_./@-]+',str(path)) or path.resolve(strict=True)!=path:
            raise ValueError('Canonical fixed paths required')
        if name!='runtime_root':
            with path.open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
            if actual!=cfg['pins'][name]:raise ValueError('Pinned input changed: '+name)
    root_owned(Path(cfg['object']))
    return cfg


def names(cfg,sid,slot):
    if not re.fullmatch('[a-f0-9]{32}',sid) or type(slot) is not int or not 0<=slot<cfg['max_slots']:
        raise ValueError('Invalid sandbox/slot identity')
    host=ipaddress.IPv4Address(int(POOL.network_address)+2*(cfg['slot_offset']+slot))
    return dict(namespace=cfg['prefix']+'-q-'+sid,host_veth='dq'+sid[:12],tap='tap0',
                host_ip=str(host),peer_ip=str(host+1),slot=slot,alias=cfg['prefix']+':'+sid)


def ns(spec,*args):return run('ip','netns','exec',spec['namespace'],*args)


def rules(cfg,spec):
    tag=['-m','comment','--comment',spec['alias']]
    interface=spec['host_veth'];uplink=cfg['uplink'];source=spec['peer_ip']+'/32'
    return [('filter','INPUT',tag+['-i',interface,'-j','DROP']),
            ('filter','FORWARD',tag+['-i',interface,'-o',uplink,'-s',source,'-j','ACCEPT']),
            ('filter','FORWARD',tag+['-i',uplink,'-o',interface,'-d',source,'-m','conntrack','--ctstate','ESTABLISHED,RELATED','-j','ACCEPT']),
            ('filter','FORWARD',tag+['-i',interface,'-j','DROP']),
            ('nat','POSTROUTING',tag+['-s',source,'-o',uplink,'-j','MASQUERADE'])]


def firewall_digest(spec):
    # Counters are operational data, not policy identity.
    text='\n'.join(line for line in ns(spec,'iptables-save').splitlines() if not line.startswith('#'))
    return hashlib.sha256(re.sub(r'\[\d+:\d+\]','[0:0]',text).encode()).hexdigest()


def link(spec,device,*,host=False):
    args=['ip','-j','link','show','dev',device] if host else ['ip','-n',spec['namespace'],'-j','link','show','dev',device]
    value=json.loads(run(*args))[0]
    if value.get('ifalias')!=spec['alias']:raise RuntimeError('Network interface ownership changed')
    return value['ifindex']


def program(spec):
    values=json.loads(ns(spec,'tc','-j','filter','show','dev','tap0','ingress'))
    # iproute2 emits both a preference/chain heading and the actual filter.
    # Headings are not programs; unexpected or unattested real filters reject.
    if not values or any(v.get('kind')!='bpf' for v in values):
        raise RuntimeError('Unexpected TC filter alongside egress guard')
    actual=[v['options'] for v in values if v.get('options')]
    if len(actual)!=1:raise RuntimeError('Expected exactly one TC egress guard')
    options=actual[0]
    # Newer iproute2 wraps the BPF identity in options.prog; older versions
    # place it in options. Never choose between two conflicting identities.
    if 'prog' in options and ('id' in options or 'tag' in options):
        raise RuntimeError('Ambiguous kernel BPF identity')
    identity=options.get('prog',options)
    if (options.get('direct-action') is not True or not isinstance(identity,dict) or
            type(identity.get('id')) is not int or identity['id']<=0 or
            not isinstance(identity.get('tag'),str) or not identity['tag']):
        raise RuntimeError('Kernel BPF attachment is not attested')
    return dict(id=identity['id'],tag=identity['tag'])


def inspect(cfg,record):
    if record['state'] not in ('READY','LENT','CLAIMED'):raise RuntimeError('Network is not prepared or is retired')
    spec=record['spec'];expected=record['identity']
    if (Path('/run/netns')/spec['namespace']).stat().st_ino!=expected['namespace_inode']:
        raise RuntimeError('Network namespace inode changed')
    if link(spec,spec['host_veth'],host=True)!=expected['host_ifindex'] or link(spec,'tap0')!=expected['tap_ifindex']:
        raise RuntimeError('Network interface identity changed')
    if program(spec)!=expected['program'] or firewall_digest(spec)!=expected['firewall_sha256']:
        raise RuntimeError('Network enforcement changed')
    for table,chain,rule in rules(cfg,spec):
        if not exists('iptables','-w','-t',table,'-C',chain,*rule):raise RuntimeError('Host boundary rule missing')
    return dict(state=record['state'],spec=spec,identity=expected,policy=cfg['policy'],enforced=True)


def prepare(cfg,sid):
    path=STATE/(sid+'.json')
    if path.exists():
        old=json.loads(path.read_text())
        if old['state']!='READY':raise RuntimeError('Network intent cannot be replayed; retire it')
        return inspect(cfg,old)
    occupied=set()
    for other in STATE.glob('*.json'):
        value=json.loads(other.read_text())
        if value['state']!='RETIRED':occupied.add(value['spec']['slot'])
    slot=next((n for n in range(cfg['max_slots']) if n not in occupied),None)
    if slot is None:raise RuntimeError('Network slots exhausted')
    spec=names(cfg,sid,slot)
    if (Path('/run/netns')/spec['namespace']).exists() or exists('ip','link','show','dev',spec['host_veth']):
        raise RuntimeError('Unowned network identity exists')
    if run('sysctl','-n','net.ipv4.ip_forward').strip()!='1':raise RuntimeError('Administrator must provision host forwarding')
    if f" dev {cfg['uplink']} " not in run('ip','-4','route','get','1.1.1.1'):
        raise RuntimeError('Configured uplink differs from actual route')
    proposed=ipaddress.IPv4Network(spec['host_ip']+'/31')
    for route in json.loads(run('ip','-j','-4','route','show','table','all')):
        destination=route.get('dst','default')
        if destination!='default' and proposed.overlaps(ipaddress.IPv4Network(destination,strict=False)):
            raise RuntimeError('Network slot overlaps an existing host route')
    record=dict(state='ALLOCATING',spec=spec);save(path,record)
    # Every interface stays down until TC and both namespace/host rules exist.
    run('ip','netns','add',spec['namespace'])
    record['namespace_inode']=(Path('/run/netns')/spec['namespace']).stat().st_ino;save(path,record)
    run('ip','link','add',spec['host_veth'],'alias',spec['alias'],'type','veth','peer','name','vpeer','netns',spec['namespace'])
    # Not every kernel applies IFLA_IFALIAS during RTM_NEWLINK creation.
    # Explicitly set and later attest it; no traffic is enabled in this gap.
    run('ip','link','set',spec['host_veth'],'alias',spec['alias'])
    run('ip','-n',spec['namespace'],'link','set','vpeer','alias',spec['alias'])
    ns(spec,'ip','tuntap','add','dev','tap0','mode','tap','user','0')
    run('ip','-n',spec['namespace'],'link','set','tap0','alias',spec['alias'],'address','06:00:dc:00:00:01')
    ns(spec,'tc','qdisc','add','dev','tap0','clsact')
    ns(spec,'tc','filter','add','dev','tap0','ingress','pref','1','bpf','da','obj',cfg['object'],'sec','classifier')
    guard=program(spec)
    for chain in ('INPUT','OUTPUT','FORWARD'):ns(spec,'iptables','-w','-P',chain,'DROP')
    ns(spec,'iptables','-w','-A','FORWARD','-i','vpeer','-o','tap0','-m','conntrack','--ctstate','ESTABLISHED,RELATED','-j','ACCEPT')
    ns(spec,'iptables','-w','-A','FORWARD','-i','tap0','-o','vpeer','-s',GUEST+'/32','-j','ACCEPT')
    ns(spec,'iptables','-w','-t','nat','-A','POSTROUTING','-s',GUEST+'/32','-o','vpeer','-j','MASQUERADE')
    # Reverse insertion preserves forward-rule order; never flush shared chains.
    for table,chain,rule in reversed(rules(cfg,spec)):
        run('iptables','-w','-t',table,'-I',chain,'1',*rule)
    run('ip','addr','add',spec['host_ip']+'/31','dev',spec['host_veth'])
    run('ip','-n',spec['namespace'],'addr','add',spec['peer_ip']+'/31','dev','vpeer')
    run('ip','-n',spec['namespace'],'addr','add',GATEWAY+'/30','dev','tap0')
    ns(spec,'sysctl','-q','-w','net.ipv4.ip_forward=1','net.ipv6.conf.all.disable_ipv6=1')
    for device in ('vpeer','tap0','lo'):run('ip','-n',spec['namespace'],'link','set',device,'up')
    run('ip','link','set',spec['host_veth'],'up')
    # A DOWN interface has no usable connected route for gateway validation.
    # TC and both firewall boundaries already enforce policy before link-up;
    # only publish READY after the default route is installed as well.
    run('ip','-n',spec['namespace'],'route','add','default','via',spec['host_ip'],'dev','vpeer')
    record.update(state='READY',identity=dict(namespace_inode=record['namespace_inode'],
        host_ifindex=link(spec,spec['host_veth'],host=True),tap_ifindex=link(spec,'tap0'),
        program=guard,firewall_sha256=firewall_digest(spec)))
    save(path,record)
    return inspect(cfg,record)


def lend(cfg,sid,record):
    if record['state']!='READY':raise RuntimeError('TAP descriptor already lent or retired; no replay')
    result=inspect(cfg,record);spec=record['spec']
    directory=Path(cfg['runtime_root'])/sid
    info=directory.lstat()
    if directory.resolve(strict=True)!=directory or not stat.S_ISDIR(info.st_mode) or info.st_uid!=cfg['uid'] or stat.S_IMODE(info.st_mode)!=0o700:
        raise PermissionError('Private runtime instance required')
    # The private root-owned device node gives AppArmor an instance-specific
    # file identity. The VMM cannot open this node or arbitrary /dev/net/tun.
    device=STATE/'taps'/sid/'tun'
    device.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if not device.exists():os.mknod(device,stat.S_IFCHR|0o600,os.makedev(10,200))
    info=device.lstat()
    if not stat.S_ISCHR(info.st_mode) or info.st_uid!=0 or info.st_rdev!=os.makedev(10,200) or info.st_mode&0o077:
        raise PermissionError('Invalid private TAP device node')
    original=os.open('/proc/self/ns/net',os.O_RDONLY);target=os.open('/run/netns/'+spec['namespace'],os.O_RDONLY)
    tap=None
    try:
        os.setns(target,0)
        tap=os.open(device,os.O_RDWR|os.O_CLOEXEC)
        fcntl.ioctl(tap,0x400454ca,struct.pack('16sH22x',b'tap0',0x0002|0x1000))
    except BaseException:
        if tap is not None:os.close(tap);tap=None
        raise
    finally:os.setns(original,0);os.close(original);os.close(target)
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
            sock.settimeout(5);sock.connect(str(directory/'network.sock'))
            _,uid,_=struct.unpack('3i',sock.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            if uid!=cfg['uid']:raise PermissionError('TAP receiver UID mismatch')
            record['state']='LENT';save(STATE/(sid+'.json'),record)
            payload=json.dumps(dict(result,sandbox_id=sid,state='LENT')).encode()
            frame=len(payload).to_bytes(4,'big')+payload
            sent=sock.sendmsg([frame],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[tap]))])
            sock.sendall(frame[sent:])
    finally:
        if tap is not None:os.close(tap)
    return dict(state='LENT')


def claim(cfg,sid,record,arguments):
    if record['state'] not in ('LENT','CLAIMED'):raise RuntimeError('Network launch claim fenced')
    if len(arguments)!=2 or not all(v.isdecimal() for v in arguments):raise ValueError('Expected PID and start time')
    saved=identity(int(arguments[0]));expected=[cfg['python'],'-I','-B',cfg['launcher'],str(Path(cfg['runtime_root'])/sid/'launch.json')]
    if (saved['uid']!=cfg['uid'] or saved['start_ticks']!=arguments[1] or saved['exe']!=cfg['python'] or
            len(saved['argv'])!=6 or saved['argv'][:5]!=expected or not saved['argv'][5].isdecimal()):
        raise RuntimeError('Network launcher identity mismatch')
    if record['state']=='CLAIMED' and record['process']!=saved:raise RuntimeError('Network already claimed by another process')
    inspect(cfg,record)
    record.update(state='CLAIMED',process=saved);save(STATE/(sid+'.json'),record)
    return dict(state='CLAIMED')


def retire(cfg,sid,record):
    if record['state']=='RETIRED':return dict(state='RETIRED')
    if record.get('process'):prove_dead(record['process'])
    # A durable fence precedes any teardown and survives partial failure.
    record['state']='RETIRING';save(STATE/(sid+'.json'),record)
    spec=record['spec'];handle=Path('/run/netns')/spec['namespace']
    if handle.exists():
        if record.get('namespace_inode')!=handle.stat().st_ino:raise RuntimeError('Namespace identity unknown or replaced; manual recovery required')
        if run('ip','netns','pids',spec['namespace']).strip():raise RuntimeError('Other processes still own namespace')
    if exists('ip','link','show','dev',spec['host_veth']):
        index=link(spec,spec['host_veth'],host=True)
        if record.get('identity') and index!=record['identity']['host_ifindex']:raise RuntimeError('Host interface replaced')
        run('ip','link','del',spec['host_veth'])
    if handle.exists():run('ip','netns','del',spec['namespace'])
    for table,chain,rule in rules(cfg,spec):
        if exists('iptables','-w','-t',table,'-C',chain,*rule):run('iptables','-w','-t',table,'-D',chain,*rule)
    device=STATE/'taps'/sid/'tun';device.unlink(missing_ok=True)
    if device.parent.exists():device.parent.rmdir()
    record['state']='RETIRED';save(STATE/(sid+'.json'),record)
    return dict(state='RETIRED')


def test_policy(cfg,record):
    """Run fixed wire packets through the *attached* kernel program, no traffic.

    No caller packet, program ID, file or repeat count enters this operation.
    """
    return check_packets(cfg,inspect(cfg,record))


def check_packets(cfg,current):
    # Internal root-only diagnostic over an already-attested program identity.
    def packet(dst,port,*,src=GUEST,proto=6,fragment=0):
        eth=bytes.fromhex('0600dc0000010600dc0000020800')
        transport=bytearray(20 if proto==6 else 8)
        struct.pack_into('!HH',transport,0,23456,port)
        if proto==6:transport[12]=0x50
        else:struct.pack_into('!H',transport,4,8)
        ip=struct.pack('!BBHHHBBH4s4s',0x45,0,20+len(transport),1,fragment,64,proto,0,
                       ipaddress.IPv4Address(src).packed,ipaddress.IPv4Address(dst).packed)
        return eth+ip+transport
    other_dns='8.8.8.8' if cfg['policy']['dns']!='8.8.8.8' else '1.1.1.1'
    cases=[('public_https',packet('8.8.8.8',443),0),
           ('metadata_denied',packet('169.254.169.254',80),2),
           ('source_spoof_denied',packet(cfg['policy']['dns'],53,src='10.0.0.1'),2),
           ('fragment_denied',packet('8.8.8.8',443,fragment=0x2000),2),
           ('configured_dns',packet(cfg['policy']['dns'],53,proto=17),0),
           ('other_dns_denied',packet(other_dns,53,proto=17),2)]
    proxy=cfg['policy']['egress_proxy']
    private=next(ip for ip in ('192.168.0.110','10.232.255.254') if not proxy or ip!=proxy['ip'])
    cases.append(('private_network_denied',packet(private,22),2))
    ipv6=bytearray(packet('8.8.8.8',443));ipv6[12:14]=b'\x86\xdd'
    cases.append(('ipv6_denied',bytes(ipv6),2))
    if proxy:cases.append(('exact_proxy',packet(proxy['ip'],proxy['port']),0))
    results=[]
    with tempfile.TemporaryDirectory(prefix='packet-check-',dir=STATE) as temp:
        file=Path(temp)/'input'
        for name,data,expected in cases:
            file.write_bytes(data)
            result=json.loads(run('bpftool','-j','prog','run','id',str(current['identity']['program']['id']),
                                  'data_in',str(file),'repeat','1'))
            if result.get('retval')!=expected:raise RuntimeError('Attached kernel packet policy failed: '+name)
            results.append(name)
    return dict(state='KERNEL_TESTED',checks=results,program=current['identity']['program'])


def main():
    global LOCK_FD
    cfg=checked_config()
    if len(sys.argv)<3 or not re.fullmatch('[a-f0-9]{32}',sys.argv[2]):raise ValueError('Expected fixed operation and sandbox ID')
    operation,sid=sys.argv[1:3];arguments=sys.argv[3:]
    with (STATE/'broker.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        # Fixed root tools inherit this open-file-description lock. If the
        # broker dies, a still-running mutation must finish before teardown.
        LOCK_FD=lock.fileno()
        if operation=='prepare' and not arguments:result=prepare(cfg,sid)
        else:
            path=STATE/(sid+'.json')
            if not path.exists():
                if operation!='retire' or arguments:raise RuntimeError('Network intent missing')
                # Fence even a delayed prepare for a not-yet-created identity.
                record=dict(state='RETIRED',spec=names(cfg,sid,0));save(path,record)
            else:record=json.loads(path.read_text())
            if record['spec']!=names(cfg,sid,record['spec']['slot']):raise RuntimeError('Network ledger identity changed')
            if operation=='inspect' and not arguments:result=inspect(cfg,record)
            elif operation=='lend' and not arguments:result=lend(cfg,sid,record)
            elif operation=='claim':result=claim(cfg,sid,record,arguments)
            elif operation=='test-policy' and not arguments:result=test_policy(cfg,record)
            elif operation=='retire' and not arguments:result=retire(cfg,sid,record)
            else:raise ValueError('Only prepare, inspect, lend, claim, test-policy, retire are supported')
        print(json.dumps(result))


if __name__=='__main__':
    try:main()
    except subprocess.CalledProcessError as exc:
        detail=exc.stderr or b''
        if isinstance(detail,bytes):detail=detail.decode(errors='replace')
        sys.stderr.write(json.dumps(dict(event='fixed-network-command-failed',command=exc.cmd,
            exit_code=exc.returncode,stderr=str(detail)[-8192:]))+'\n');raise SystemExit(1)
