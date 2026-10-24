"""Render reviewable deployment files and explicitly initialize configuration.

Rendering writes only the requested output. These helpers never install the
result or start services; administrator application is a separate action.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
from urllib.parse import urlsplit
from dsec.host.configuration import load_config, initial_configuration


def systemd_quote(value):
    # systemd expands specifiers even inside quotes; never interpolate a shell.
    return json.dumps(str(value).replace('%', '%%'))


def render_units(cfg, out):
    out = Path(out).resolve()
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    def command(arguments):
        group = cfg.get('service_group')
        if group:
            arguments = ['/usr/bin/sg', group, '-c', 'exec '+shlex.join(arguments)]
        return ':'+' '.join(systemd_quote(x) for x in arguments)
    base = [sys.executable, '-I', '-B', '-m', 'dsec_host', '--config', cfg['_file'], 'run']
    name = 'dsec-'+cfg['instance']
    units = {}
    for role in ('sandbox', 'worker'):
        unit = name+'-'+role+'.service'
        before = ''
        if role == 'sandbox':
            admin = command([sys.executable, '-I', '-B', '-m', 'service_admin', '--root',
                             str(Path(cfg['state_root'])/'sandboxes')])
            before = 'ExecStartPre='+admin+'\nExecStop='+admin+'\n'
        units[unit] = ('[Unit]\nDescription=DSec '+role+' ('+cfg['instance']+')\n'+
            'StartLimitIntervalSec=60\nStartLimitBurst=5\n'+
            ('After='+name+'-sandbox.service\nWants='+name+'-sandbox.service\n' if role == 'worker' else '')+
            '\n[Service]\nType=simple\n'+before+'ExecStart='+command(base+[role])+'\n'+
            'Restart=always\nRestartSec=2\nTimeoutStartSec=120\nTimeoutStopSec=60\n'+
            ('KillMode=process\n' if role == 'sandbox' else '')+
            'UMask=0077\n\n[Install]\nWantedBy=default.target\n')
    for name, text in units.items():
        destination = out/name
        if destination.exists() and destination.read_text() != text:
            raise FileExistsError('Refusing to overwrite a different unit: '+str(destination))
        destination.write_text(text)
    return {'units':[str(out/name) for name in units], 'scope':'trusted single-user user services'}


def render_privileges(cfg, out, user, slot_offset):
    import grp
    import ipaddress
    import pwd
    from dsec.host import privileged_helper as privileged_netns_helper, node_probe
    from dsec.host.install_template import INSTALLER
    account = pwd.getpwnam(user)
    group = grp.getgrnam('kvm')
    if account.pw_uid <= 0 or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', user):
        raise ValueError('Expected an unprivileged runtime account')
    if account.pw_gid != group.gr_gid and user not in group.gr_mem:
        raise ValueError('Runtime user must already have kvm group membership')
    if not cfg['sandbox'].get('microvm_environment_catalog'):
        raise ValueError('Network privilege bundle requires an environment catalog')
    network = cfg.get('network', {})
    slots = network.get('max_slots', cfg['sandbox'].get('capacity', 4))
    if not 0 <= slot_offset <= 32768-slots:
        raise ValueError('Network slot range exceeds the reserved address pool')
    name = 'dsec-'+cfg['instance']+'-netns'
    helper_path = '/usr/local/libexec/'+name+'-helper'
    config_path = '/etc/dsec/'+name+'.json'
    state = '/var/lib/dsec/'+name
    privileged = {'uid':account.pw_uid, 'gid':group.gr_gid,
                  'runtime_root':str(Path(cfg['state_root'])/'sandboxes'),
                  'firecracker':cfg['sandbox']['binary'],
                  'uplink':cfg['scheduler']['network_interface'], 'dns':network.get('dns', '1.1.1.1')}
    ipaddress.IPv4Address(privileged['dns'])
    for key, path in [('firecracker', cfg['sandbox']['binary']),
                      ('dax_firecracker', network.get('dax_binary') or
                       cfg['sandbox'].get('tb2_verifier_dax_binary'))]:
        if path:
            privileged[key] = str(Path(path).resolve(strict=True))
            privileged[key+'_sha256'] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    proxy = cfg['sandbox'].get('egress_proxy_url')
    if proxy:
        url = urlsplit(proxy)
        privileged['egress_proxy'] = {'ip':str(ipaddress.IPv4Address(url.hostname)),
                                     'port':url.port or (443 if url.scheme == 'https' else 80)}
    source = Path(privileged_netns_helper.__file__).read_text()
    replacements = {'CONFIG = Path("/etc/dsec-netns-helper.json")':'CONFIG = Path('+repr(config_path)+')',
                    'STATE = Path("/var/lib/dsec-netns")':'STATE = Path('+repr(state)+')',
                    'SLOT_OFFSET = 0':'SLOT_OFFSET = '+str(slot_offset),
                    'MAX_SLOTS = 32768':'MAX_SLOTS = '+str(slots)}
    for old, new in replacements.items():
        if source.count(old) != 1:
            raise RuntimeError('Network helper template differs from this generator')
        source = source.replace(old, new)
    compile(source, helper_path, 'exec')
    contents = {'netns-helper.py':source.encode(),
                'helper-config.json':(json.dumps(privileged, indent=2)+'\n').encode(),
                'sudoers':(user+' ALL=(root) NOPASSWD: '+helper_path+' *\n').encode()}
    specs = [(filename, target, mode, hashlib.sha256(contents[filename]).hexdigest())
             for filename, target, mode in [('netns-helper.py', helper_path, 0o755),
             ('helper-config.json', config_path, 0o644),
             ('sudoers', '/etc/sudoers.d/'+name, 0o440)]]
    installer = INSTALLER.replace('__SPECS__', repr(specs)).replace('__STATE__', repr(state))
    installer = installer.replace('__GUARD_SOURCE__', repr(Path(node_probe.__file__).read_text()))
    installer = installer.replace('__RUNTIME_IDENTITY__', repr((user, account.pw_uid, group.gr_gid)))
    compile(installer, 'install-privileges.py', 'exec')
    connected = {key:value for key,value in cfg.items() if key != '_file'}
    connected['network'] = {'helper':helper_path, 'max_slots':slots, 'dns':privileged['dns']}
    if privileged.get('dax_firecracker'):
        connected['network']['dax_binary'] = privileged['dax_firecracker']
    contents['install-privileges.py'] = installer.encode()
    contents['host-with-network.json'] = (json.dumps(connected, indent=2)+'\n').encode()
    destination = Path(out).resolve()
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    for filename, blob in contents.items():
        path = destination/filename
        path.write_bytes(blob)
        path.chmod(0o600)
    return {'bundle':str(destination), 'install':str(destination/'install-privileges.py'),
            'configuration':str(destination/'host-with-network.json'),
            'slot_range':[slot_offset, slot_offset+slots-1],
            'storage_provisioning':'existing ublk service must authorize this sandbox root'}


def initialize_configuration(path, **options):
    """Initialize once using initial_configuration's keyword options."""
    path = Path(path)
    if path.exists():
        raise FileExistsError('Configuration already exists')
    cfg = initial_configuration(**options)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Reserve the final path without overwriting a concurrent initializer.
    with path.open('x') as stream:
        os.chmod(path, 0o600)
        json.dump(cfg, stream, indent=2)
    load_config(path)
    return {'configuration':str(path.resolve())}
