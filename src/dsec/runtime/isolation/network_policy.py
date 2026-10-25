"""Pinned IPv4 egress policy compiled for TC; no runtime compiler or packet RPC."""
import ipaddress
import json

GUEST_IP='169.254.110.2'
GATEWAY='169.254.110.1'
GUEST_MAC='06:00:dc:00:00:02'
GATEWAY_MAC='06:00:dc:00:00:01'
# Explicit wire ranges, independent of Python's changing is_private semantics.
DENIED=('0.0.0.0/8','10.0.0.0/8','100.64.0.0/10','127.0.0.0/8',
        '169.254.0.0/16','172.16.0.0/12','192.0.0.0/24','192.0.2.0/24',
        '192.88.99.0/24','192.168.0.0/16','198.18.0.0/15','198.51.100.0/24',
        '203.0.113.0/24','224.0.0.0/3')


def policy(value):
    if not isinstance(value,dict) or set(value)!={'dns','egress_proxy'}:
        raise ValueError('Explicit DNS and optional egress_proxy required')
    def address(value):
        if not isinstance(value,str):raise ValueError('IPv4 string required')
        parsed=ipaddress.IPv4Address(value)
        if str(parsed)!=value or parsed.is_unspecified or parsed.is_multicast or parsed.is_loopback:
            raise ValueError('Canonical unicast IPv4 required')
        if any(parsed in ipaddress.IPv4Network(n) for n in ('0.0.0.0/8','169.254.0.0/16','224.0.0.0/3')):
            raise ValueError('Link-local service exceptions are forbidden')
        return value
    address(value['dns'])
    proxy=value['egress_proxy']
    if proxy is not None:
        if (not isinstance(proxy,dict) or set(proxy)!={'ip','port'} or
                type(proxy['port']) is not int or not 1<=proxy['port']<=65535):
            raise ValueError('One explicit TCP proxy endpoint required')
        address(proxy['ip'])
    return json.loads(json.dumps(value,sort_keys=True))


def source(value):
    """One predicate is exercised natively and compiled into the kernel program.

    Ethernet/IP/transport checks precede service exceptions. No VLAN, IPv6, IP
    options or fragments are admitted. Return traffic is conntracked separately.
    """
    value=policy(value)
    ranges='\n'.join(f'    if ((dst & {int(n.netmask)}u)=={int(n.network_address)}u) return 0;'
                     for n in map(ipaddress.IPv4Network,DENIED))
    proxy=value['egress_proxy']
    exception=(f'    if (proto==6 && dst=={int(ipaddress.IPv4Address(proxy["ip"]))}u && port=={proxy["port"]}) return 1;'
               if proxy else '')
    return '''/* Generated from an administrator-reviewed immutable policy. */
static unsigned short be16(const unsigned char *p) { return ((unsigned short)p[0]<<8)|p[1]; }
static unsigned int be32(const unsigned char *p) { return ((unsigned int)p[0]<<24)|((unsigned int)p[1]<<16)|((unsigned int)p[2]<<8)|p[3]; }
static int mac(const unsigned char *p, unsigned char last) { return p[0]==6 && p[1]==0 && p[2]==220 && p[3]==0 && p[4]==0 && p[5]==last; }
static __attribute__((always_inline)) inline int permitted(const unsigned char *p, const unsigned char *end) {
    if (p+14>end || !mac(p+6,2)) return 0;
    unsigned short ether=be16(p+12);
    if (ether==0x0806) {
        if (p+42>end) return 0;
        return be16(p+14)==1 && be16(p+16)==0x0800 && p[18]==6 && p[19]==4 &&
               (be16(p+20)==1 || be16(p+20)==2) && mac(p+22,2) &&
               be32(p+28)==0xa9fe6e02u && be32(p+38)==0xa9fe6e01u;
    }
    if (ether!=0x0800 || p+34>end || !mac(p,1)) return 0;
    if (p[14]!=0x45 || (be16(p+20)&0xbfff)!=0 || be32(p+26)!=0xa9fe6e02u) return 0;
    unsigned short total=be16(p+16);
    if (total<28 || total>1500 || p+14+total>end) return 0;
    unsigned char proto=p[23];
    if (proto==6) {
        if (p+54>end || total<40 || (p[46]>>4)<5 || ((p[46]>>4)*4)>total-20) return 0;
    } else if (proto==17) {
        if (p+42>end || be16(p+38)!=total-20) return 0;
    } else return 0;
    unsigned int dst=be32(p+30);
    unsigned short port=be16(p+36);
''' + f'''    if (dst=={int(ipaddress.IPv4Address(value['dns']))}u && port==53) return 1;
{exception}
    if (port==53) return 0;
{ranges}
    return 1;
}}
''' + '''
#ifdef DSEC_POLICY_NATIVE
int dsec_packet_allowed(const unsigned char *p, unsigned long size) { return permitted(p,p+size); }
#else
#include <linux/bpf.h>
__attribute__((section("classifier"),used))
int dsec_egress(struct __sk_buff *skb) {
    const unsigned char *p=(void *)(unsigned long)skb->data;
    const unsigned char *end=(void *)(unsigned long)skb->data_end;
    return permitted(p,end) ? 0 /* TC_ACT_OK */ : 2 /* TC_ACT_SHOT */;
}
char dsec_license[] __attribute__((section("license"),used))="GPL";
#endif
'''
