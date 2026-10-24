"""Initialize or refresh only this dedicated Compose quorum's credentials.

Runs in the unprivileged initializer container, never on an existing etcd
deployment. Passwords stay in private files and HTTP bodies, not command args.
The regular installed IAM implementation owns bootstrap/admission state.
"""
import base64
import json
import os
from pathlib import Path
import ssl
import sys
import urllib.error
import urllib.request

from dsec.contracts.cluster import Quota
from dsec.control.coordination import EtcdStore
from dsec.control.iam import IAM
from dsec._credentials import secret_file


class EtcdError(RuntimeError):
    def __init__(self, operation, code, reason=None):
        super().__init__(f'etcd initialization rejected {operation} (code {code})')
        self.code = code
        self.reason = reason
        self.missing = code in (5, 9) and reason in (
            'etcdserver: user name not found', 'etcdserver: role name not found')


def token_exports(config):
    enabled = config.get('export_node_token', True)
    if type(enabled) is not bool:
        raise ValueError('export_node_token must be boolean')
    return ('/secrets', '/node-secrets') if enabled else ('/secrets',)


def initialize(config):
    exports = token_exports(config)
    context = ssl.create_default_context(cafile='/initialize/ca.crt')
    context.load_cert_chain('/initialize/client.crt', '/initialize/client.key')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                        urllib.request.HTTPSHandler(context=context))
    endpoint = config['endpoints'][0]
    token = None

    def rpc(operation, payload):
        request = urllib.request.Request(endpoint+'/v3/'+operation,
            json.dumps(payload).encode(), {'Content-Type':'application/json',
                **({'Authorization':token} if token else {})})
        try:
            with opener.open(request, timeout=10) as response:
                value = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                value = json.loads(exc.read(65536))
            except (ValueError, UnicodeDecodeError):
                raise EtcdError(operation, exc.code) from None
        if 'error' in value or 'code' in value:
            raise EtcdError(operation, value.get('code'), value.get('message', value.get('error')))
        return value

    def optional(operation, payload):
        try:
            return rpc(operation, payload)
        except EtcdError as exc:
            if not exc.missing:  # Only a proven missing identity permits creation.
                raise
            return None

    def authenticate_root():
        return rpc('auth/authenticate', {'name':'root',
            'password':secret_file('/initialize/root-password')})['token']

    try:
        enabled = rpc('auth/status', {}).get('enabled')
    except EtcdError as exc:
        # After auth is enabled, even auth/status requires an identity.
        # Only this exact response permits authenticating an existing root.
        if exc.code != 9 or exc.reason != 'etcdserver: user name not found':
            raise
        token = authenticate_root()
        enabled = rpc('auth/status', {}).get('enabled')
    if not enabled:
        if optional('auth/user/get', {'name':'root'}) is None:
            rpc('auth/user/add', {'name':'root', 'password':secret_file('/initialize/root-password')})
        if optional('auth/role/get', {'role':'root'}) is None:
            rpc('auth/role/add', {'name':'root'})
        root = rpc('auth/user/get', {'name':'root'})
        if 'root' not in root.get('roles', []):
            rpc('auth/user/grant', {'user':'root','role':'root'})
        rpc('auth/enable', {})
    if token is None:
        token = authenticate_root()
    role = optional('auth/role/get', {'role':'dsec-cluster'})
    if role is None:
        rpc('auth/role/add', {'name':'dsec-cluster'})
        role = {'perm':[]}
    prefix = config['prefix'].encode()
    permission = dict(permType='READWRITE', key=base64.b64encode(prefix).decode(),
        range_end=base64.b64encode(prefix[:-1]+bytes([prefix[-1]+1])).decode())
    if not role.get('perm'):
        rpc('auth/role/grant', {'name':'dsec-cluster','perm':permission})
    elif role['perm'] != [permission]:
        raise ValueError('Existing etcd role does not match this isolated key prefix')
    user = optional('auth/user/get', {'name':'dsec'})
    if user is None:
        rpc('auth/user/add', {'name':'dsec','password':secret_file('/initialize/dsec-password')})
        user = {'roles':[]}
    if set(user.get('roles', [])) - {'dsec-cluster'}:
        raise ValueError('Existing etcd user has unexpected roles')
    if 'dsec-cluster' not in user.get('roles', []):
        rpc('auth/user/grant', {'user':'dsec','role':'dsec-cluster'})
    credential = rpc('auth/authenticate', {'name':'dsec',
        'password':secret_file('/initialize/dsec-password')})['token']
    for directory in exports:
        path = Path(directory)/'etcd-token'
        temp = path.with_suffix('.pending')
        temp.write_text(credential+'\n'); temp.chmod(0o600); temp.replace(path)
    options = config['coordination']
    iam = IAM(EtcdStore(**options, token=credential))
    iam.bootstrap(secret_file('/secrets/bootstrap-token'), Quota(**config['root_quota']))
    print(json.dumps({'status':'initialized','auth_enabled':True,
                      'token_ttl_seconds':86400,'prefix':config['prefix']}), flush=True)


if __name__ == '__main__':
    os.umask(0o077)
    initialize(json.loads(Path(sys.argv[1]).read_text()))
