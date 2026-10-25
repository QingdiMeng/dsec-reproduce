"""Bounded Linux cgroup controller acceptance in a dedicated delegated service.

No root, VM, GPU, host Docker, firewall or existing-service changes. Run with
Delegate=cpu memory pids, DelegateSubgroup=edge. Only three newly-created groups
and their direct test children are used. OOM targets one 48 MiB child group.
"""
import argparse
import errno
import json
import os
from pathlib import Path
import subprocess
import sys


def verify(root,output):
    root=root.resolve(strict=True)
    expected='0::/'+str(root.relative_to('/sys/fs/cgroup'))+'/edge'
    if os.geteuid()==0 or Path('/proc/self/cgroup').read_text().strip()!=expected:
        raise PermissionError('Run inside this dedicated unprivileged delegated service/edge')
    if (root/'cgroup.procs').read_text().strip():raise RuntimeError('Delegated root is populated')
    if not {'cpu','memory','pids'}<=set((root/'cgroup.controllers').read_text().split()):
        raise RuntimeError('Controllers unavailable')
    (root/'cgroup.subtree_control').write_text('+cpu +memory +pids')
    report=dict(status='running',checks=[],scope=__doc__)
    groups=[]
    def check(name,condition,**facts):
        if not condition:raise AssertionError(name)
        report['checks'].append(dict(name=name,**facts))
    def group(name,**limits):
        path=root/name;path.mkdir();groups.append(path)
        for key,value in limits.items():(path/key.replace('_','.')).write_text(str(value))
        return path
    def child(path,program):
        code='import os,sys,errno,time\nopen('+repr(str(path/'cgroup.procs'))+',"w").write("0\\n")\n'+program
        return subprocess.run([sys.executable,'-I','-c',code],capture_output=True,text=True,timeout=8)
    def counters(path,name):
        return {k:int(v) for k,v in (line.split() for line in (path/name).read_text().splitlines())}
    try:
        memory=group('probe-memory',memory_max=48*2**20,memory_swap_max=0,memory_oom_group=1,pids_max=16)
        result=child(memory,'a=bytearray(128*2**20); print("unexpected",flush=True)')
        events=counters(memory,'memory.events')
        check('memory_limit_oom_kills_only_child',result.returncode==-9 and events['oom_kill']>=1,
              returncode=result.returncode,events=events)
        pids=group('probe-pids',pids_max=8,memory_max=64*2**20,memory_swap_max=0)
        result=child(pids,'''r,w=os.pipe(); children=[]; refused=False
try:
 for _ in range(32):
  try: pid=os.fork()
  except OSError as e:
   if e.errno!=errno.EAGAIN:raise
   refused=True;break
  if pid==0:
   os.close(w);os.read(r,1);os._exit(0)
  children.append(pid)
finally:
 os.close(w)
 for pid in children:os.waitpid(pid,0)
print("REFUSED" if refused else "UNBOUNDED")
''')
        events=counters(pids,'pids.events')
        check('pids_limit_refuses_additional_processes',result.returncode==0 and result.stdout.strip()=='REFUSED'
              and events['max']>=1,events=events)
        cpu=group('probe-cpu',cpu_max='10000 100000',memory_max=64*2**20,pids_max=16)
        result=child(cpu,'deadline=time.monotonic()+0.5\nwhile time.monotonic()<deadline:pass')
        events=counters(cpu,'cpu.stat')
        check('cpu_quota_throttles_busy_child',result.returncode==0 and events['nr_throttled']>=1,events=events)
        report['status']='passed'
    except BaseException as exc:
        report.update(status='failed',error=type(exc).__name__+': '+str(exc))
        raise
    finally:
        errors=[]
        for path in groups:
            if 'populated 0' not in (path/'cgroup.events').read_text().splitlines():
                errors.append(str(path));continue
            path.rmdir()
        report['cleanup_errors']=errors
        if errors:report['status']='failed'
        output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(dict(status=report['status'],checks=len(report['checks']),cleanup_errors=errors)))
    if report['status']!='passed':raise RuntimeError('Kernel controller acceptance failed')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cgroup-root',type=Path,required=True);parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();verify(args.cgroup_root,args.out)
