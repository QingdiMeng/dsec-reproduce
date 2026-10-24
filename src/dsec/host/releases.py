"""Offline, side-by-side runtime packages; explicit starts, never service control."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import venv

from dsec._persistence import atomic_json
from dsec._release import installed_release
from dsec.contracts.release import release_ref
from dsec.host.release_artifacts import inspect_wheel
from dsec.host.services import private_directory, check_private_directory


def stage(wheel, sha256, root):
    wheel, root = Path(wheel).resolve(strict=True), Path(root).resolve()
    ref = inspect_wheel(wheel,sha256)  # Validate before any write.
    private_directory(root)
    with (root/'.stage.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        target = root/ref['build']
        if target.exists():
            record = json.loads((target/'release.json').read_text())
            if record['release'] != ref or record['wheel_sha256'] != sha256:
                raise ValueError('Staged release identity conflict')
            verify(target)
            return record
        target.mkdir(mode=0o700)
        # Failed/interrupted staging remains unselected and is never reused as
        # complete. Keep its files for diagnosis instead of deleting evidence.
        package = target/wheel.name
        package.write_bytes(wheel.read_bytes())
        inspect_wheel(package,sha256)
        venv.EnvBuilder(with_pip=True,symlinks=False).create(target/'venv')
        python = target/'venv/bin/python'
        subprocess.run([str(python),'-I','-m','pip','install','--no-index','--no-deps',str(package)],
                       check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        observed = query(python)
        if observed != ref:raise ValueError('Installed runtime differs from the validated wheel')
        record = dict(release=ref,wheel_sha256=sha256)
        atomic_json(target/'release.json',record)
        return record


def query(python):
    result = subprocess.run([str(python),'-I','-B','-c',
        'import json;from dsec._release import installed_release;print(json.dumps(installed_release()))'],
        check=True,capture_output=True,text=True)
    return release_ref(json.loads(result.stdout))


def verify(directory):
    directory = Path(directory).resolve(strict=True)
    check_private_directory(directory)
    record = json.loads((directory/'release.json').read_text())
    expected = release_ref(record['release'])
    if query(directory/'venv/bin/python') != expected:
        raise ValueError('Staged runtime payload changed')
    return expected


def permitted_release(member, ref):
    """Startup fence, not permission to resume scheduling or rewrite state."""
    ref = release_ref(ref)
    if member is None or member['state']=='JOINING':return
    if member['state']=='RETIRED':raise ValueError('Retired Node cannot restart')
    pending = member.get('upgrade')
    if pending:
        if member['state']!='MAINTENANCE' or ref not in (pending['source'],pending['target']):
            raise ValueError('Release is outside the pending maintenance plan')
    elif member.get('active_release') is not None and member['active_release'] != ref:
        raise ValueError('Only the committed Node release may restart')


def run(directory, role, config, node_config=None):
    directory, config = Path(directory).resolve(strict=True), Path(config).resolve(strict=True)
    ref = verify(directory)
    python = directory/'venv/bin/python'
    # Re-enter from the selected installed code, even when the operator CLI is
    # an older installation. No shell, pointer mutation or fallback executable.
    command = [str(python),'-I','-B','-m','dsec.host.releases','launch','--role',role,'--config',str(config),
               '--expected-build',ref['build']]
    if node_config is not None:command += ['--node-config',str(Path(node_config).resolve(strict=True))]
    elif role!='controller':raise ValueError('Node roles require their existing Node configuration')
    os.execv(str(python),command)


def launch(role, config, expected_build, node_config=None):
    actual = installed_release()
    if actual is None or actual['build'] != expected_build:
        raise ValueError('Selected runtime identity changed before launch')
    if role != 'controller':
        from dsec.host.cluster import components
        from dsec.control.node_lifecycle import NodeLifecycle
        node_options=json.loads(Path(node_config).read_text())
        gateway=components(node_options,'node')
        permitted_release(NodeLifecycle(gateway.iam).read(gateway.node_id),actual)
    if role in ('sandbox','worker'):
        from dsec.host.configuration import load_config
        from dsec.host.services import run_service
        cfg=load_config(config)
        if node_options['node']['edge_socket'] != str(Path(cfg['state_root'])/'sandboxes/service.sock'):
            raise ValueError('Host state does not match the declared Node Edge')
        if role=='worker' and node_options['node'].get('worker_socket') != str(Path(cfg['state_root'])/'worker/worker.sock'):
            raise ValueError('Host state does not match the declared Node episode service')
        if cfg['worker'].get('container_catalog'):
            from dsec.runtime.backends import container_agent
            cfg['worker']['container_agent']=container_agent.__file__
        run_service(cfg,role)
    else:
        from dsec.host.cluster import main as cluster_main
        sys.argv=['dsec-cluster',role,'--config',str(config)]
        cluster_main()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    install=sub.add_parser('stage');install.add_argument('--wheel',type=Path,required=True)
    install.add_argument('--sha256',required=True);install.add_argument('--root',type=Path,required=True)
    check=sub.add_parser('verify');check.add_argument('--release-dir',type=Path,required=True)
    for name in ('run','launch'):
        command=sub.add_parser(name)
        command.add_argument('--role',choices=('sandbox','worker','node','controller'),required=True)
        command.add_argument('--config',type=Path,required=True);command.add_argument('--node-config',type=Path)
        if name=='run':command.add_argument('--release-dir',type=Path,required=True)
        else:command.add_argument('--expected-build',required=True)
    args=parser.parse_args()
    os.umask(0o077)
    if args.action=='stage':print(json.dumps(stage(args.wheel,args.sha256,args.root)))
    elif args.action=='verify':print(json.dumps(verify(args.release_dir)))
    elif args.action=='run':run(args.release_dir,args.role,args.config,args.node_config)
    else:launch(args.role,args.config,args.expected_build,args.node_config)


if __name__=='__main__':main()
