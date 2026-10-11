"""Real three-member etcd process-failure gate; no physical multi-host claim.

Uses explicitly selected local Docker, an immutable image digest and a new
exclusive evidence directory. Owns only named test containers/network. Runtime
driver and Linux Node acceptance are separate gates.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from dsec.contracts.cluster import CoordinationUnavailable, Quota, QuotaExceeded
from dsec.control.coordination import EtcdStore
from dsec.control.iam import IAM


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--image', required=True, help='etcd image pinned by repository@sha256 digest')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9./_-]+@sha256:[0-9a-f]{64}', args.image):
        parser.error('Immutable image digest required')
    root = args.root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    prefix = 'dsec-ha-' + uuid.uuid4().hex[:10]
    names = [prefix+'-'+str(n) for n in range(3)]
    report = {'status':'running', 'image':args.image, 'checks':[], 'cleanup_errors':[],
              'boundary':'Real three-member etcd process failures on one Docker host; not whole-host HA or real Node acceptance'}
    def write():
        (root/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    def docker(*arguments, check=True):
        result = subprocess.run(['docker', *arguments], capture_output=True, text=True, timeout=60)
        with (root/'docker.log').open('a') as stream:
            stream.write(json.dumps(arguments)+'\n'+result.stdout+result.stderr)
        if check and result.returncode:
            raise RuntimeError('Docker operation failed: '+result.stderr)
        return result
    def wait(function, seconds=20):
        deadline = time.monotonic()+seconds
        error = None
        while time.monotonic() < deadline:
            try:
                return function()
            except CoordinationUnavailable as exc:
                error = exc
                time.sleep(.2)
        raise RuntimeError('etcd gate did not become ready') from error
    def passed(name, **evidence):
        report['checks'].append(dict(name=name, passed=True, **evidence)); write()
        print(json.dumps({'passed':name, **evidence}), flush=True)
    network = False
    containers = []
    write()
    try:
        docker('network','create',prefix); network=True
        initial = ','.join(f'{name}=http://{name}:2380' for name in names)
        endpoints = []
        for name in names:
            docker('run','-d','--name',name,'--network',prefix,'--memory','256m',
                '-p','127.0.0.1::2379', args.image,'/usr/local/bin/etcd',
                '--name',name,'--data-dir','/data','--listen-client-urls','http://0.0.0.0:2379',
                '--advertise-client-urls',f'http://{name}:2379',
                '--listen-peer-urls','http://0.0.0.0:2380',
                '--initial-advertise-peer-urls',f'http://{name}:2380',
                '--initial-cluster',initial,'--initial-cluster-token',prefix)
            containers.append(name)
            port = json.loads(docker('inspect',name).stdout)[0]['NetworkSettings']['Ports']['2379/tcp'][0]['HostPort']
            endpoints.append('http://127.0.0.1:'+port)
        report['endpoints'] = endpoints
        store = EtcdStore(endpoints, prefix='/'+prefix+'/', timeout=1, allow_loopback_http=True)
        iam = IAM(store)
        token = uuid.uuid4().hex+uuid.uuid4().hex
        quota = Quota(4,4,2048,4096,4)
        demand = Quota(1,1,512,1024,1)
        wait(lambda:iam.bootstrap(token, quota))
        passed('real-quorum-bootstrap')
        replicas = [IAM(EtcdStore(endpoints, prefix='/'+prefix+'/', timeout=1,
                                 allow_loopback_http=True)) for _ in range(2)]
        def attempt(index):
            try:
                return replicas[index%2].admit(token, 'root', f'{index:032x}', 'd'*64,
                    'linux1', 'microvm', demand, args={})
            except QuotaExceeded:
                return None
        with ThreadPoolExecutor(max_workers=12) as executor:
            results = [r for r in executor.map(attempt, range(1,13)) if r]
        assert len(results)==4, results
        assert store.read('projects/root').value['usage']['concurrency']==4
        passed('parallel-iam-replicas-quota', attempted=12, admitted=4)
        receipt = results[0]
        iam.bind('root',receipt['request_id'],'linux1','a'*12)
        passed('owner-binding')
        docker('stop','-t','3',names[0])
        wait(lambda:replicas[1].authorize(token,'root','sandbox.read'))
        assert replicas[1].lookup('root',receipt['request_id'])['sandbox_id']=='a'*12
        passed('one-member-loss-preserves-service-and-binding')
        docker('stop','-t','3',names[1])
        try:
            replicas[1].authorize(token,'root','sandbox.read')
            raise AssertionError('Authorization succeeded without quorum')
        except CoordinationUnavailable:
            pass
        passed('quorum-loss-fails-closed')
        docker('start',names[1])
        wait(lambda:replicas[1].authorize(token,'root','sandbox.read'))
        assert replicas[1].lookup('root',receipt['request_id'])['state']=='BOUND'
        assert store.read('projects/root').value['usage']['concurrency']==4
        passed('quorum-restoration-preserves-held-grants')
        for _ in range(2):
            replicas[1].release('root',receipt['request_id'],'linux1',sandbox_id='a'*12)
        assert store.read('projects/root').value['usage']['concurrency']==3
        passed('duplicate-release-charges-once')
        report['status']='passed'
    except BaseException as exc:
        report.update(status='failed',error=repr(exc))
        raise
    finally:
        for name in containers:
            log = docker('logs',name,check=False)
            (root/(name+'.log')).write_text(log.stdout+log.stderr)
            if docker('rm','-f',name,check=False).returncode:
                report['cleanup_errors'].append(name)
        if network and docker('network','rm',prefix,check=False).returncode:
            report['cleanup_errors'].append(prefix)
        report['cleanup'] = dict(containers_remaining=[name for name in names
            if docker('inspect',name,check=False).returncode==0],
            network_remaining=docker('network','inspect',prefix,check=False).returncode==0)
        if report['cleanup_errors'] or report['cleanup']['containers_remaining'] or report['cleanup']['network_remaining']:
            report['status']='failed'
        report['source_sha256'] = {str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest()
            for directory in ('src/dsec/control','src/dsec/contracts')
            for path in (ROOT/directory).glob('*.py') if ' ' not in path.name}
        write()
    raise SystemExit(report['status']!='passed')


if __name__=='__main__':
    main()
