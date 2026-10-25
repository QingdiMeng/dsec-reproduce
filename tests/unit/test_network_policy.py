"""Execute the actual TC predicate as native C against constructed wire packets."""
import ctypes
import ipaddress
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from dsec.runtime.isolation.network_policy import source,policy,GUEST_MAC,GATEWAY_MAC


def packet(destination,port=443,*,source_ip='169.254.110.2',protocol=6,fragment=0):
    eth=bytes.fromhex(GATEWAY_MAC.replace(':','')+GUEST_MAC.replace(':',''))+b'\x08\x00'
    transport=bytearray(20 if protocol==6 else 8)
    struct.pack_into('!HH',transport,0,23456,port)
    if protocol==6:transport[12]=0x50
    else:struct.pack_into('!H',transport,4,8)
    ip=struct.pack('!BBHHHBBH4s4s',0x45,0,20+len(transport),1,fragment,64,protocol,0,
                   ipaddress.IPv4Address(source_ip).packed,ipaddress.IPv4Address(destination).packed)
    return eth+ip+transport


class NetworkPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('cc'):raise unittest.SkipTest('native C compiler required for packet predicate')
        cls.temp=tempfile.TemporaryDirectory();root=Path(cls.temp.name)
        c=root/'policy.c';c.write_text(source(dict(dns='192.168.0.1',egress_proxy=dict(ip='192.168.0.104',port=18888))))
        library=root/'policy.so'
        subprocess.run(['cc','-shared','-fPIC','-O2','-Wall','-Wextra','-Werror','-DDSEC_POLICY_NATIVE',str(c),'-o',str(library)],check=True,capture_output=True)
        cls.lib=ctypes.CDLL(str(library));cls.test=cls.lib.dsec_packet_allowed
        cls.test.argtypes=[ctypes.c_void_p,ctypes.c_ulong];cls.test.restype=ctypes.c_int

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def allowed(self,frame):return bool(self.test(ctypes.create_string_buffer(frame),len(frame)))

    def test_public_tcp_udp_and_exact_dns_proxy_exceptions(self):
        for frame in (packet('8.8.8.8'),packet('8.8.8.8',123,protocol=17),
                      packet('192.168.0.1',53),packet('192.168.0.1',53,protocol=17),
                      packet('192.168.0.104',18888)):
            self.assertTrue(self.allowed(frame))
        for frame in (packet('192.168.0.1',80),packet('192.168.0.104',18889),
                      packet('192.168.0.104',18888,protocol=17),packet('8.8.8.8',53)):
            self.assertFalse(self.allowed(frame))

    def test_host_peer_metadata_and_reserved_ranges_denied(self):
        for ip in ('0.1.1.1','10.1.2.3','100.64.0.1','127.0.0.1','169.254.169.254',
                   '172.31.0.1','192.168.0.110','192.0.0.1','192.0.2.1','198.18.0.1',
                   '198.51.100.2','203.0.113.1','224.0.0.1','255.255.255.255'):
            with self.subTest(ip=ip):self.assertFalse(self.allowed(packet(ip)))

    def test_spoofing_fragments_options_ipv6_vlan_and_truncation_denied(self):
        frames=[packet('8.8.8.8',source_ip='10.0.0.1'),packet('192.168.0.1',53,source_ip='10.0.0.1'),
                packet('8.8.8.8',fragment=1),packet('8.8.8.8',fragment=0x2000),packet('8.8.8.8',protocol=1)]
        for offset,value in ((0,255),(6,255),(12,0x86),(14,0x46),(46,0x40)):
            frame=bytearray(packet('8.8.8.8'));frame[offset]=value;frames.append(bytes(frame))
        frame=bytearray(packet('8.8.8.8'));frame[12:14]=b'\x81\x00';frames.append(bytes(frame))
        frame=packet('8.8.8.8');frames.extend(frame[:n] for n in range(len(frame)))
        for frame in frames:self.assertFalse(self.allowed(frame))
        self.assertTrue(self.allowed(packet('8.8.8.8',fragment=0x4000)))

    def test_arp_only_for_own_address_and_gateway(self):
        frame=bytearray(bytes.fromhex('ffffffffffff'+GUEST_MAC.replace(':',''))+b'\x08\x06'+
            struct.pack('!HHBBH',1,0x0800,6,4,1)+bytes.fromhex(GUEST_MAC.replace(':',''))+
            ipaddress.IPv4Address('169.254.110.2').packed+b'\0'*6+ipaddress.IPv4Address('169.254.110.1').packed)
        self.assertTrue(self.allowed(bytes(frame)))
        for offset in (14,18,22,28,38):
            invalid=frame.copy();invalid[offset]^=1;self.assertFalse(self.allowed(bytes(invalid)))

    def test_policy_rejects_implicit_or_metadata_service_exceptions(self):
        for value in ({'dns':'1.1.1.1'},dict(dns='169.254.169.254',egress_proxy=None),
                      dict(dns='1.1.1.1',egress_proxy=dict(ip='127.0.0.1',port=80)),
                      dict(dns='1.1.1.1',egress_proxy=dict(ip='192.168.0.104',port=True))):
            with self.assertRaises(ValueError):policy(value)


if __name__=='__main__':unittest.main()
