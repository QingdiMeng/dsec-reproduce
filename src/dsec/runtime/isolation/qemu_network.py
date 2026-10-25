"""Unprivileged, one-shot TAP capability client; root owns policy and slots."""
import array
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import subprocess
from dsec.runtime.isolation.network_policy import policy


def configuration(value):
    if (not isinstance(value,dict) or set(value)!={'helper','uplink','slot_offset','max_slots','policy'} or
            not re.fullmatch('/usr/local/libexec/dsec-[a-z0-9-]+-qemu-network',value['helper']) or
            not re.fullmatch('[a-zA-Z0-9_.-]{1,15}',value['uplink']) or
            type(value['slot_offset']) is not int or type(value['max_slots']) is not int or
            not 0<=value['slot_offset']<32768 or not 1<=value['max_slots']<=32768-value['slot_offset']):
        raise ValueError('Fixed helper, uplink and bounded network slots required')
    return dict(value,policy=policy(value['policy']))


class QemuNetwork:
    def __init__(self,settings,directory):
        self.policy=configuration(settings);self.directory=Path(directory)
        self.record=None;self.fd=None

    def broker(self,operation,*args):
        try:result=subprocess.run(['/usr/bin/sudo','-n',self.policy['helper'],operation,self.directory.name,*map(str,args)],
            capture_output=True,text=True,check=True,timeout=30,env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C'})
        except subprocess.CalledProcessError as exc:
            raise RuntimeError('Network broker '+operation+' failed: '+str(exc.stderr)[-4096:]) from exc
        return json.loads(result.stdout)

    def prepare(self):
        helper=Path(self.policy['helper']);info=helper.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:
            raise PermissionError('Install reviewed network broker first')
        prepared=self.broker('prepare')
        if prepared['state']!='READY' or prepared['policy']!=self.policy['policy'] or not prepared['enforced']:
            raise RuntimeError('Network policy not prepared')
        path=self.directory/'network.sock'
        if len(os.fsencode(path))>=104:raise ValueError('Private network socket path too long')
        descriptors=[]
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as listener:
            listener.settimeout(15);listener.bind(str(path));listener.listen(1)
            command=['/usr/bin/sudo','-n',self.policy['helper'],'lend',self.directory.name]
            child=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C'})
            try:
                with listener.accept()[0] as channel:
                    channel.settimeout(5)
                    _,uid,_=struct.unpack('3i',channel.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                    if uid!=0:raise PermissionError('TAP sender must be the root broker')
                    data,ancillary,flags,_=channel.recvmsg(16388,socket.CMSG_SPACE(16))
                    for level,kind,blob in ancillary:
                        if level==socket.SOL_SOCKET and kind==socket.SCM_RIGHTS:
                            fds=array.array('i');fds.frombytes(blob[:len(blob)//fds.itemsize*fds.itemsize]);descriptors.extend(fds)
                    if flags&(socket.MSG_CTRUNC|socket.MSG_TRUNC) or len(descriptors)!=1:
                        raise RuntimeError('Expected exactly one TAP descriptor')
                    while len(data)<4:
                        part=channel.recv(4-len(data))
                        if not part:raise EOFError('Incomplete TAP metadata')
                        data+=part
                    size=int.from_bytes(data[:4],'big')
                    if not 1<=size<=16384:raise ValueError('Invalid TAP metadata size')
                    while len(data)<size+4:
                        part=channel.recv(size+4-len(data))
                        if not part:raise EOFError('Incomplete TAP metadata')
                        data+=part
                    value=json.loads(data[4:])
                    if (len(data)!=size+4 or value.pop('sandbox_id',None)!=self.directory.name or
                            value.get('state')!='LENT' or value.get('policy')!=prepared['policy'] or
                            value.get('identity')!=prepared['identity'] or value.get('spec')!=prepared['spec'] or
                            value.get('enforced') is not True):
                        raise RuntimeError('TAP delivery identity mismatch')
                stdout,stderr=child.communicate(timeout=5)
                if child.returncode or json.loads(stdout).get('state')!='LENT':
                    raise RuntimeError('TAP broker failed: '+stderr.decode(errors='replace')[-1000:])
                info=os.fstat(descriptors[0])
                if not stat.S_ISCHR(info.st_mode) or info.st_uid!=0 or info.st_rdev!=os.makedev(10,200):
                    raise PermissionError('Broker did not deliver a private tun device')
                self.fd=descriptors.pop();os.set_inheritable(self.fd,False)
                self.record=dict(policy=self.policy,identity=prepared['identity'],spec=prepared['spec'],fd=self.fd)
                return self.record
            except BaseException:
                self.close();raise
            finally:
                if child.poll() is None:child.kill();child.wait(timeout=5)
                child.stdout.close();child.stderr.close();path.unlink(missing_ok=True)
                for fd in descriptors:os.close(fd)

    def restore(self,value):
        if value['policy']!=self.policy or type(value['fd']) is not int or value['fd']<3:
            raise ValueError('Persisted network identity changed')
        self.record=value

    def claim(self,saved):
        if self.broker('claim',saved['pid'],saved['start_ticks'])['state']!='CLAIMED':
            raise RuntimeError('Network launch claim rejected')

    def attest(self):
        value=self.broker('inspect')
        if (self.record is None or value['identity']!=self.record['identity'] or value['spec']!=self.record['spec'] or
                value['policy']!=self.policy['policy'] or value['state']!='CLAIMED' or not value['enforced']):
            raise RuntimeError('Network policy or kernel identity changed')
        return value

    def close(self):
        if self.fd is not None:os.close(self.fd);self.fd=None

    def retire(self):
        self.close()
        if self.broker('retire')['state']!='RETIRED':raise RuntimeError('Network retirement incomplete')
