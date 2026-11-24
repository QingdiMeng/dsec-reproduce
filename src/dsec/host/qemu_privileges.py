"""Render a reviewable AppArmor-only broker installation, never apply it."""
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import sys
import subprocess
import tempfile

from dsec.host import node_probe, qemu_profile_helper
from dsec.host.install_template import INSTALLER
from dsec.runtime.backends.qemu import QemuArtifact, uefi_profile_files
from dsec.runtime.backends.qemu_container import ExecutionCatalog
from dsec.runtime.isolation import qemu_launch


def render(cfg,out,user,*,clang='clang',layer_root=None):
    account=pwd.getpwnam(user);group=grp.getgrnam('kvm')
    if account.pw_uid!=os.getuid() or account.pw_uid<=0 or not re.fullmatch('[a-z_][a-z0-9_-]{0,31}',user):
        raise ValueError('Render as the named unprivileged runtime user')
    if group.gr_gid not in os.getgrouplist(user,account.pw_gid):
        raise ValueError('Pre-provision the runtime kvm group')
    catalog=ExecutionCatalog(cfg['sandbox']['qemu_execution_catalog'])
    if not catalog.host_isolation:raise ValueError('Explicit host_isolation policy required')
    name='dsec-'+cfg['instance']+'-qemu-profile'
    helper='/usr/local/libexec/'+name;config='/etc/dsec/'+name+'.json';state='/var/lib/dsec/'+name
    if catalog.host_isolation['helper']!=helper:raise ValueError('Broker belongs to another host instance')
    binaries={json.dumps(item['binary'],sort_keys=True) for item in catalog.environments.values()}
    kernels={json.dumps(item['kernel'],sort_keys=True) for item in catalog.environments.values() if 'kernel' in item}
    if len(binaries)!=1 or len(kernels)>1:
        raise ValueError('One broker binds one QEMU binary/kernel pair; use separate Nodes for different pairs')
    artifacts={name:json.loads(next(iter(values))) for name,values in (('binary',binaries),('kernel',kernels)) if values}
    for item in artifacts.values():QemuArtifact(**item).verify()
    python=str(Path(sys.executable).resolve(strict=True));launcher=str(Path(qemu_launch.__file__).resolve(strict=True))
    runtime_root=cfg['worker'].get('container_root',str(Path(cfg['state_root'])/'containers'))
    settings=dict(uid=account.pw_uid,prefix='dsec-'+cfg['instance'],runtime_root=runtime_root,
        python=python,launcher=launcher,**{name:item['path'] for name,item in artifacts.items()},
        pins={**{name:item['sha256'] for name,item in artifacts.items()},
              'python':hashlib.sha256(Path(python).read_bytes()).hexdigest(),
              'launcher':hashlib.sha256(Path(launcher).read_bytes()).hexdigest()})
    boots=[]
    for item in catalog.environments.values():
        files=uefi_profile_files(item)
        if files is not None and files not in boots:
            qemu_profile_helper.validate_uefi_files(settings,files,check_files=True)
            for pin in files.values():QemuArtifact(**pin).verify()
            boots.append(files)
    if boots:
        settings['uefi_boots']=boots
    shared=[layer for item in catalog.environments.values() for layer in item.get('readonly_layers',[])]
    if layer_root is not None:
        root=Path(layer_root)
        if root.is_symlink() or root.resolve(strict=True)!=root or not root.is_dir():
            raise ValueError('Canonical existing shared publication root required')
        settings['layer_root']=str(root)
    if shared:
        if layer_root is None:
            raise ValueError('Explicit shared layer publication root required; render with layer_root')
        from dsec.runtime.backends.qemu import QemuReadOnlyLayer
        for layer in shared:
            qemu_profile_helper.checked_layers(settings,[layer],check_files=True)
            if not Path(layer['path']).is_relative_to(settings['layer_root']):raise ValueError('Layer outside publication root')
            QemuReadOnlyLayer(**layer).verify()
    network_contents={};network_specs=[];network_state=None
    if catalog.network is not None:
        from dsec.host import qemu_network_helper
        from dsec.runtime.isolation.network_policy import source as policy_source
        network=catalog.network;network_name='dsec-'+cfg['instance']+'-qemu-network'
        network_helper='/usr/local/libexec/'+network_name
        network_config='/etc/dsec/'+network_name+'.json'
        network_object='/etc/dsec/'+network_name+'.o'
        network_state='/var/lib/dsec/'+network_name
        if network['helper']!=network_helper:raise ValueError('Network broker belongs to another host instance')
        settings['tun_root']=network_state+'/taps'
        c=policy_source(network['policy'])
        with tempfile.TemporaryDirectory(prefix='dsec-tc-build-') as temp:
            cfile=Path(temp)/'egress.c';obj=Path(temp)/'egress.o';cfile.write_text(c)
            subprocess.run([clang,'-O2','-Wall','-Werror','-target','bpf','-I/usr/include/x86_64-linux-gnu',
                            '-c',str(cfile),'-o',str(obj)],check=True,capture_output=True,timeout=30)
            object_bytes=obj.read_bytes()
        if object_bytes[:4]!=b'\x7fELF' or int.from_bytes(object_bytes[18:20],'little')!=247:
            raise ValueError('Compiler did not produce an ELF eBPF object')
        netcfg=dict(uid=account.pw_uid,prefix=settings['prefix'],runtime_root=runtime_root,
            **{key:network[key] for key in ('uplink','slot_offset','max_slots','policy')},
            python=python,launcher=launcher,object=network_object,
            pins={key:settings['pins'][key] for key in ('python','launcher')})
        netcfg['pins']['object']=hashlib.sha256(object_bytes).hexdigest()
        net_source=Path(qemu_network_helper.__file__).read_text()
        for before,after in (("CONFIG=Path('/etc/dsec-qemu-network.json')",'CONFIG=Path('+repr(network_config)+')'),
                             ("STATE=Path('/var/lib/dsec-qemu-network')",'STATE=Path('+repr(network_state)+')')):
            if net_source.count(before)!=1:raise RuntimeError('Network broker template changed')
            net_source=net_source.replace(before,after)
        compile(net_source,network_helper,'exec')
        network_contents={'network-helper.py':net_source.encode(),'network-config.json':(json.dumps(netcfg,indent=2)+'\n').encode(),
                          'egress.o':object_bytes,'egress.c':c.encode()}
        network_specs=[('network-helper.py',network_helper,0o755),('network-config.json',network_config,0o644),('egress.o',network_object,0o644)]
    qemu_profile_helper.validate(settings)
    source=Path(qemu_profile_helper.__file__).read_text()
    for before,after in (("CONFIG = Path('/etc/dsec-qemu-profile.json')",'CONFIG = Path('+repr(config)+')'),
                         ("STATE = Path('/var/lib/dsec-qemu-profile')",'STATE = Path('+repr(state)+')')):
        if source.count(before)!=1:raise RuntimeError('Profile broker template changed')
        source=source.replace(before,after)
    contents={'profile-helper.py':source.encode(),'helper-config.json':(json.dumps(settings,indent=2)+'\n').encode(),
              'sudoers':(user+' ALL=(root) NOPASSWD: '+helper+' *\n').encode()}
    contents.update(network_contents)
    if network_state:
        contents['sudoers']+=(user+' ALL=(root) NOPASSWD: '+catalog.network['helper']+' *\n').encode()
    specs=[(file,target,mode,hashlib.sha256(contents[file]).hexdigest()) for file,target,mode in (
        ('profile-helper.py',helper,0o755),('helper-config.json',config,0o644),
        ('sudoers','/etc/sudoers.d/'+name,0o440),*network_specs)]
    installer=INSTALLER.replace('__SPECS__',repr(specs)).replace('__STATE__',repr(state))
    installer=installer.replace('__GUARD_SOURCE__',repr(Path(node_probe.__file__).read_text()))
    installer=installer.replace('__RUNTIME_IDENTITY__',repr((user,account.pw_uid,group.gr_gid)))
    installer=installer.replace('netns-helper.py','profile-helper.py').replace('instance network privileges','instance AppArmor privileges')
    installer=installer.replace("namespace['observe'](administrator=True)",
                                "namespace['observe'](network_required=False, administrator=True)")
    installer=installer.replace('report = observe()','report = observe(network_required=False)')
    installer=installer.replace('Network state must be root-only','AppArmor state must be root-only')
    if network_state:
        installer=installer.replace("directory(STATE, mode=0o700)","directory(STATE, mode=0o700)\n    directory("+repr(network_state)+", mode=0o700)")
        installer=installer.replace("compile(blobs['profile-helper.py'], 'profile-helper.py', 'exec')",
            "compile(blobs['profile-helper.py'], 'profile-helper.py', 'exec')\n    compile(blobs['network-helper.py'], 'network-helper.py', 'exec')")
    compile(source,helper,'exec');compile(installer,'install-qemu-privileges.py','exec')
    contents['install-qemu-privileges.py']=installer.encode()
    example_layers=[] if boots else next((item['readonly_layers'] for item in catalog.environments.values() if item.get('readonly_layers')),[])
    contents['sample.profile']=qemu_profile_helper.render_profile(
        settings,'a'*32,example_layers,boots[0] if boots else None).encode()
    destination=Path(out).resolve();destination.mkdir(mode=0o700,parents=True,exist_ok=False)
    for filename,blob in contents.items():
        path=destination/filename;path.write_bytes(blob);path.chmod(0o600)
    return dict(bundle=str(destination),install=str(destination/'install-qemu-privileges.py'),
                scope=('AppArmor and isolated TAP brokers; no service or network changes during installation'
                       if network_state else 'AppArmor broker only; no service, cgroup, Docker or network changes'),
                targets=[entry[1] for entry in specs])
