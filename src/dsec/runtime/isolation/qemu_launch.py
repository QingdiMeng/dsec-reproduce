"""Unprivileged, gated exec helper. Never use as a root or setuid launcher."""
import ctypes
import fcntl
import json
import os
from pathlib import Path
import resource
import stat
import sys


def launch(path, reply_fd):
    if os.geteuid()==0:raise PermissionError('The QEMU launcher must not run as root')
    flags=fcntl.fcntl(reply_fd,fcntl.F_GETFD)
    fcntl.fcntl(reply_fd,fcntl.F_SETFD,flags|fcntl.FD_CLOEXEC)
    os.write(reply_fd,b'READY\n')
    if sys.stdin.buffer.read(1)!=b'G':
        raise RuntimeError('Launch owner lost before committed exec permission')
    plan=json.loads(Path(path).read_text())
    if plan['uid']!=os.getuid():raise PermissionError('Launcher identity changed')
    group=Path(plan['group'])
    fd=os.open(group,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        info=os.fstat(fd)
        if (info.st_dev,info.st_ino)!=(plan['device'],plan['inode']):
            raise RuntimeError('Cgroup identity changed before exec')
        target=os.open('cgroup.procs',os.O_WRONLY|os.O_NOFOLLOW,dir_fd=fd)
        try:os.write(target,b'0\n')
        finally:os.close(target)
    finally:os.close(fd)
    library=ctypes.CDLL('libapparmor.so.1',use_errno=True)
    library.aa_change_onexec.argtypes=[ctypes.c_char_p]
    library.aa_change_onexec.restype=ctypes.c_int
    if library.aa_change_onexec(plan['profile'].encode())!=0:
        raise OSError(ctypes.get_errno(),'AppArmor exec transition rejected')
    libc=ctypes.CDLL(None,use_errno=True)
    if libc.prctl(38,1,0,0,0)!=0:raise OSError(ctypes.get_errno(),'NoNewPrivs rejected')
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    os.execve(plan['argv'][0],plan['argv'],{'PATH':'/usr/bin:/bin','LANG':'C'})


if __name__=='__main__':
    reply=int(sys.argv[2])
    try:launch(sys.argv[1],reply)
    except BaseException as exc:
        os.write(reply,(type(exc).__name__+': '+str(exc)[:1000]).encode())
        raise SystemExit(1)
