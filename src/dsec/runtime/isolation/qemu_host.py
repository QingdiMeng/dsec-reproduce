"""Enforced QEMU host limits. Edge journals remain the lifecycle authority.

Requires an explicitly delegated systemd service subtree and a fixed, scoped
AppArmor broker. No best-effort fallback, process migration after exec, or
health-based resource release. This does not enable production networking.
"""
import json
import os
from pathlib import Path
import re
import select
import stat
import subprocess
import sys
import time

from dsec._persistence import atomic_json

CONTROLLERS = {'cpu','memory','pids'}


def canonical(value):
    path=Path(value)
    if (not path.is_absolute() or not re.fullmatch(r'/[A-Za-z0-9_./@-]+',str(path))
            or '..' in path.parts or path.resolve() != path):
        raise ValueError('Canonical safe host isolation path required')
    return path


def configuration(value):
    if (not isinstance(value,dict) or set(value)!={'cgroup_root','helper','memory_overhead_mb','pids_max'}
            or type(value['memory_overhead_mb']) is not int or not 128<=value['memory_overhead_mb']<=4096
            or type(value['pids_max']) is not int or not 32<=value['pids_max']<=1024):
        raise ValueError('Invalid explicit QEMU host isolation policy')
    root=canonical(value['cgroup_root']);helper=canonical(value['helper'])
    if not root.is_relative_to('/sys/fs/cgroup') or not root.name.endswith('.service'):
        raise ValueError('Host isolation requires a delegated service cgroup')
    if not re.fullmatch(r'/usr/local/libexec/dsec-[a-z0-9-]+-qemu-profile',str(helper)):
        raise ValueError('An instance-scoped administrator helper is required')
    return dict(value)


class HostIsolation:
    def __init__(self, policy, directory, memory_mb, cpus):
        self.policy=configuration(policy);self.directory=canonical(directory)
        if not re.fullmatch('[a-f0-9]{32}',self.directory.name):
            raise ValueError('Host isolation requires the instance sandbox identity')
        self.root=Path(policy['cgroup_root']);self.group=self.root/('vm-'+self.directory.name)
        self.limits={'memory.max':str((memory_mb+policy['memory_overhead_mb'])*2**20),
            'memory.swap.max':'0','memory.oom.group':'1','pids.max':str(policy['pids_max']),
            'cpu.max':f'{cpus*100000} 100000'}
        self.readonly_layers=[]
        self.record=None
        self.created=False

    def broker(self, operation, *args):
        result=subprocess.run(['/usr/bin/sudo','-n',self.policy['helper'],operation,
            self.directory.name,*map(str,args)],check=True,text=True,stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,timeout=15,env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C'})
        return json.loads(result.stdout)

    def check_root(self):
        root=canonical(self.root);info=root.stat()
        if info.st_uid!=os.getuid() or not stat.S_ISDIR(info.st_mode):
            raise PermissionError('Runtime-owned delegated cgroup required')
        current=Path('/sys/fs/cgroup'+Path('/proc/self/cgroup').read_text().strip().removeprefix('0::'))
        if current != root/'edge':
            raise PermissionError('Start Edge with Delegate=cpu memory pids and DelegateSubgroup=edge')
        if (root/'cgroup.procs').read_text().strip():
            raise RuntimeError('Delegated root must contain no processes')
        if not CONTROLLERS <= set((root/'cgroup.controllers').read_text().split()):
            raise PermissionError('Required host controllers are not delegated')
        if not CONTROLLERS <= set((root/'cgroup.subtree_control').read_text().split()):
            (root/'cgroup.subtree_control').write_text('+cpu +memory +pids')

    def prepare(self):
        helper=Path(self.policy['helper']);info=helper.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode & 0o022:
            raise PermissionError('Install the reviewed root-owned AppArmor broker first')
        self.check_root()
        self.group.mkdir()  # Never reuse a group from a previous boot intent.
        self.created=True
        for name,value in self.limits.items():
            (self.group/name).write_text(value)
        self.check_limits()
        info=self.group.stat()
        prepared=self.broker('prepare',*([json.dumps(self.readonly_layers,separators=(',',':'))] if self.readonly_layers else []))
        if prepared['state']!='PREPARED':
            raise RuntimeError('AppArmor profile is not prepared')
        self.record=dict(policy=self.policy,group=str(self.group),device=info.st_dev,inode=info.st_ino,
                         limits=self.limits,profile=prepared['profile'],profile_sha256=prepared['sha256'])
        if self.readonly_layers:self.record['readonly_layers']=self.readonly_layers
        return self.record

    def restore(self, record):
        if (record['policy']!=self.policy or record['group']!=str(self.group)
                or record['limits']!=self.limits or record.get('readonly_layers',[])!=self.readonly_layers):
            raise ValueError('Saved host isolation policy changed')
        self.record=record

    def check_limits(self):
        if canonical(self.group)!=self.group:
            raise ValueError('Host cgroup identity changed')
        if self.record:
            info=self.group.stat()
            if (info.st_dev,info.st_ino)!=(self.record['device'],self.record['inode']):
                raise RuntimeError('Host cgroup inode changed; retirement fenced')
        for name,value in self.limits.items():
            if (self.group/name).read_text().strip()!=value:
                raise RuntimeError('Host cgroup limit not enforced: '+name)

    def launch(self, argv, log, save_identity, *, on_spawn=None, pass_fds=(), claim=None):
        """Wait for a cooperative child, journal its identity, then permit exec.

        EOF on the parent's input pipe makes an uncommitted child exit. The
        error pipe closes on exec; no Python preexec_fn runs in a threaded Edge.
        """
        plan=dict(self.record,argv=argv,uid=os.getuid())
        atomic_json(self.directory/'launch.json',plan)
        reader,writer=os.pipe()
        from dsec.runtime.isolation import qemu_launch
        command=[str(Path(sys.executable).resolve()),'-I','-B',str(Path(qemu_launch.__file__).resolve()),
                 str(self.directory/'launch.json'),str(writer)]
        child=None
        try:
            child=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=log,stderr=subprocess.STDOUT,
                pass_fds=(writer,*pass_fds),env={'PATH':'/usr/bin:/bin','LANG':'C'})
            if on_spawn is not None:on_spawn(child)
            os.close(writer);writer=-1
            deadline=time.monotonic()+10
            def read():
                remaining=deadline-time.monotonic()
                if remaining<=0 or not select.select([reader],[],[],remaining)[0]:
                    raise TimeoutError('Confined launcher exceeded its deadline')
                return os.read(reader,4096)
            if read()!=b'READY\n':
                raise RuntimeError('Confined launcher did not establish its gate')
            from dsec.runtime.registry import identity
            saved=identity(child.pid)
            if saved['argv']!=command or saved['exe']!=command[0] or saved['uid']!=os.getuid():
                raise RuntimeError('Confined launcher identity mismatch')
            save_identity(saved,command)
            if claim is not None:claim(saved)
            claimed=self.broker('claim',child.pid,saved['start_ticks'])
            if claimed['state']!='CLAIMED':
                raise RuntimeError('AppArmor launch claim rejected')
            child.stdin.write(b'G');child.stdin.flush();child.stdin.close()
            error=read()
            if error:
                raise RuntimeError('Confined exec rejected: '+error.decode(errors='replace')[:1000])
            return child
        except BaseException:
            if child is not None:
                if child.stdin and not child.stdin.closed:child.stdin.close()
                if child.poll() is None:child.terminate()
                child.wait(timeout=5)
            raise
        finally:
            os.close(reader)
            if writer>=0:os.close(writer)

    def attest(self, pid):
        self.check_limits()
        proc=Path('/proc')/str(pid)
        if (proc/'cgroup').read_text().strip()!='0::/'+str(self.group.relative_to('/sys/fs/cgroup')):
            raise RuntimeError('VMM is outside its enforced cgroup')
        if (proc/'attr/current').read_text().strip()!=self.record['profile']+' (enforce)':
            raise RuntimeError('VMM AppArmor profile is not enforcing')
        status=dict(line.split(':',1) for line in (proc/'status').read_text().splitlines() if ':' in line)
        if (status['NoNewPrivs'].strip()!='1' or status['Seccomp'].strip()!='2'
                or int(status['CapEff'],16) or int(status['CapPrm'],16)):
            actual={key:status[key].strip() for key in ('NoNewPrivs','Seccomp','CapEff','CapPrm')}
            raise RuntimeError('VMM privilege/seccomp state mismatch: '+json.dumps(actual,sort_keys=True))
        return dict(enforced=True,profile=self.record['profile'],cgroup=str(self.group),limits=self.limits)

    def retire(self):
        if self.group.exists():
            if self.record is None and not self.created:
                raise RuntimeError('Unowned host cgroup exists; retirement fenced')
            canonical(self.group)
            if self.record:
                info=self.group.stat()
                if (info.st_dev,info.st_ino)!=(self.record['device'],self.record['inode']):
                    raise RuntimeError('Host cgroup inode changed; retirement fenced')
            if 'populated 0' not in (self.group/'cgroup.events').read_text().splitlines():
                raise RuntimeError('Host cgroup still populated; resources retained')
            # Capture peak/OOM proof before removing the kernel's counters.
            # Missing optional kernel fields stay absent, never invented as 0.
            fields={name:(self.group/name).read_text() for name in
                    ('memory.current','memory.peak','memory.events','memory.stat','cpu.stat','io.stat')
                    if (self.group/name).is_file()}
            atomic_json(self.directory/'host-resources-retired.json',
                        dict(schema=1,cgroup=str(self.group),counters=fields))
        value=self.broker('retire')
        if value['state']!='RETIRED':
            raise RuntimeError('AppArmor profile retirement not proved')
        if self.group.exists():self.group.rmdir()
