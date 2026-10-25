"""Standalone trusted-guest IPv4 setup; no DHCP, shell, sudo or host access."""
import json
from pathlib import Path
import socket
import struct


def attribute(kind,payload):
    value=struct.pack('HH',4+len(payload),kind)+payload
    return value+b'\0'*((-len(value))%4)


def configure(settings):
    # Called once by init in a fresh VM, before Docker or task execution.
    index=socket.if_nametoindex('eth0')
    with socket.socket(socket.AF_NETLINK,socket.SOCK_RAW,socket.NETLINK_ROUTE) as channel:
        channel.bind((0,0));channel.settimeout(5)
        sequence=0
        def request(kind,flags,payload):
            nonlocal sequence
            sequence+=1
            channel.send(struct.pack('IHHII',16+len(payload),kind,1|4|flags,sequence,0)+payload)
            while True:
                data=channel.recv(65536);offset=0
                while offset+16<=len(data):
                    length,reply,_,seq,_=struct.unpack_from('IHHII',data,offset)
                    if length<16 or offset+length>len(data):raise ValueError('Malformed netlink response')
                    if seq==sequence and reply==2:
                        error=struct.unpack_from('i',data,offset+16)[0]
                        if error:raise OSError(-error,'Guest network setup rejected')
                        return
                    offset+=(length+3)&~3
        request(16,0,struct.pack('BBHiII',0,0,0,socket.if_nametoindex('lo'),1,1))
        # RTM_NEWLINK: UP and a fixed MTU; RTM_NEWADDR and default RTM_NEWROUTE.
        request(16,0,struct.pack('BBHiII',0,0,0,index,1,1)+attribute(4,struct.pack('I',1400)))
        local=socket.inet_aton('169.254.110.2')
        request(20,0x400|0x200,struct.pack('BBBBI',socket.AF_INET,30,0,0,index)+attribute(1,local)+attribute(2,local))
        request(24,0x400|0x200,struct.pack('BBBBBBBBI',socket.AF_INET,0,0,0,254,4,0,1,0)+
                attribute(5,socket.inet_aton('169.254.110.1'))+attribute(4,struct.pack('I',index)))
    Path('/etc/resolv.conf').write_text('nameserver '+str(socket.inet_ntoa(socket.inet_aton(settings['dns'])))+'\n')


if __name__=='__main__':
    import sys
    configure(json.loads(Path(sys.argv[1]).read_text())['network'])
