"""Standalone read-only Linux capability probe, also embedded in the root installer.

No device creation, VM creation, namespace mutation, service or network calls.
Container markers are diagnostic only; actual capabilities decide eligibility.
"""
import errno
import os
from pathlib import Path
import shutil
import stat
import sys


NETWORK_CAPS = (1 << 12) | (1 << 21)  # CAP_NET_ADMIN and CAP_SYS_ADMIN


def observe(*, network_required=True, administrator=False):
    checks = []
    facts = dict(platform=sys.platform, uid=os.getuid(), euid=os.geteuid())

    def check(name, function):
        try:
            detail = function()
            checks.append(dict(check=name, ok=True, detail=detail))
        except PermissionError as exc:
            if (name == 'namespace_context' and not administrator and os.geteuid() != 0
                    and exc.errno in (errno.EACCES, errno.EPERM)):
                checks.append(dict(check=name, ok=None, error='PID 1 namespace visibility requires administrator verification',
                                   errno=exc.errno, remedy='administrator-check-and-runtime-helper'))
            else:
                checks.append(dict(check=name, ok=False, error=str(exc)[:300],
                                   errno=exc.errno, remedy='host-administrator-or-provider'))
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            checks.append(dict(check=name, ok=False, error=str(exc)[:300],
                               errno=getattr(exc, 'errno', None), remedy='host-administrator-or-provider'))

    def require(condition, message):
        if not condition:
            raise RuntimeError(message)

    check('linux', lambda: require(sys.platform == 'linux', 'Linux is required for a microVM Node'))
    if sys.platform != 'linux':
        return dict(ok=False, checks=checks, facts=facts, sandbox_boot_verified=False)

    import fcntl

    def device(path, major, minor, operation):
        info = os.stat(path)
        require(stat.S_ISCHR(info.st_mode) and os.major(info.st_rdev) == major
                and os.minor(info.st_rdev) == minor, 'Unexpected device identity: '+path)
        fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        try:
            return operation(fd)
        finally:
            os.close(fd)

    def kvm(fd):
        require(fcntl.ioctl(fd, 0xAE00, 0) == 12, 'KVM_GET_API_VERSION did not return 12')
        return 'KVM API accessible; no VM created'

    check('kvm', lambda: device('/dev/kvm', 10, 232, kvm))
    facts['container_markers'] = [name for name in ('/.dockerenv', '/run/.containerenv') if Path(name).exists()]

    if network_required:
        def capabilities():
            values = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
            for key in ('CapBnd', 'CapEff'):
                facts[key] = values[key].strip()
            facts['seccomp'] = values.get('Seccomp', 'unknown').strip()
            facts['no_new_privs'] = values['NoNewPrivs'].strip()
            require(int(facts['CapBnd'], 16) & NETWORK_CAPS == NETWORK_CAPS,
                    'Capability bounding set lacks NET_ADMIN/SYS_ADMIN; sudo cannot add them in this context')
            if administrator:
                require(os.geteuid() == 0 and int(facts['CapEff'], 16) & NETWORK_CAPS == NETWORK_CAPS,
                        'Initializer requires effective root NET_ADMIN/SYS_ADMIN')
            elif os.geteuid() != 0:
                require(facts['no_new_privs'] == '0',
                        'NoNewPrivs prevents this runtime account from using the sudo network helper')
            return 'required network capability bounds present'

        def user_namespace():
            entries = [tuple(map(int, line.split())) for line in Path('/proc/self/uid_map').read_text().splitlines()]
            require(entries == [(0, 0, 4294967295)],
                    'Remapped user namespace is unsupported by the host-scoped network helper')
            return 'initial UID mapping'

        def namespace_context():
            for name in ('mnt', 'net'):
                require(os.readlink('/proc/self/ns/'+name) == os.readlink('/proc/1/ns/'+name),
                        'Network helper requires the visible PID 1 '+name+' namespace')
            return 'matches visible PID 1; this alone does not prove physical-host authority'

        def tun(fd):
            fcntl.ioctl(fd, 0x800454CF, bytearray(4), True)  # TUNGETFEATURES; no TUNSETIFF
            return 'TUN device accessible; no interface created'

        check('network_capabilities', capabilities)
        check('user_namespace', user_namespace)
        check('namespace_context', namespace_context)
        check('tun', lambda: device('/dev/net/tun', 10, 200, tun))
        check('ipv4_forwarding', lambda: require(Path('/proc/sys/net/ipv4/ip_forward').read_text().strip() == '1',
              'Host IPv4 forwarding must be provisioned; the initializer will not change it'))
        for tool in ('ip', 'iptables', 'sysctl', 'sudo', 'visudo'):
            check('tool:'+tool, lambda tool=tool: require(shutil.which(tool) is not None,
                  'Host administrator must provision '+tool))
    return dict(ok=all(item['ok'] is not False for item in checks), checks=checks, facts=facts,
                administrator_checks_required=[item['check'] for item in checks if item['ok'] is None],
                sandbox_boot_verified=False)


def require_eligible(report):
    if not report['ok']:
        names = ', '.join(item['check'] for item in report['checks'] if item['ok'] is False)
        raise RuntimeError('MicroVM Node host prerequisites failed: '+names+'; inspect dsec-host preflight')
