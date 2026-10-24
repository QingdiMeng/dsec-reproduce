"""etcd v3 conditional JSON transactions. No local production fallback."""
import base64
from dataclasses import dataclass
import json
import http.client
import math
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request

from dsec.contracts.cluster import CoordinationUnavailable
from dsec._tls import TLSFiles, ReloadingOpener


def encode(value):
    return base64.b64encode(value.encode()).decode()


@dataclass(frozen=True)
class Record:
    key: str
    value: dict | None
    revision: int


class EtcdStore:
    def __init__(self, endpoints, *, prefix='/dsec/v1/', ca_file=None,
                 cert_file=None, key_file=None, token=None, timeout=5,
                 allow_loopback_http=False, authentication=None, security_file=None):
        self.endpoints = tuple(endpoint.rstrip('/') for endpoint in endpoints)
        if not self.endpoints or not prefix.startswith('/') or not prefix.endswith('/'):
            raise ValueError('Explicit endpoints and isolated etcd prefix required')
        for endpoint in self.endpoints:
            parsed = urllib.parse.urlsplit(endpoint)
            if (parsed.username or parsed.password or parsed.query or parsed.fragment
                    or parsed.path or not parsed.hostname):
                raise ValueError('Invalid etcd endpoint')
            if parsed.scheme != 'https' and not (allow_loopback_http
                    and parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', '::1', 'localhost')):
                raise ValueError('etcd requires verified TLS; HTTP is only for explicit loopback tests')
        self.prefix, self.token, self.timeout = prefix, token, timeout
        if isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError('Finite etcd timeout in (0, 60] required')
        self.endpoint_lock, self.preferred = threading.Lock(), 0
        tls=TLSFiles(ca_file=ca_file,cert_file=cert_file,key_file=key_file,security_file=security_file)
        self.context=tls.snapshot().context
        self.opener=ReloadingOpener(tls)
        self.credential = None
        if authentication is not None:
            from dsec.control.etcd_auth import EtcdCredential
            if not isinstance(authentication,dict) or set(authentication)-{
                    'username','password_file','identity_file','renew_before_seconds','retry_seconds'}:
                raise ValueError('Invalid etcd authentication configuration')
            self.credential = EtcdCredential(self.endpoints,self.opener,token=token,
                                            timeout=timeout,**authentication)

    def _rpc(self, path, payload, *, retry_unknown=True):
        data = json.dumps(payload, separators=(',', ':')).encode()
        with self.endpoint_lock:
            preferred = self.preferred
        for index in list(range(preferred, len(self.endpoints))) + list(range(preferred)):
            endpoint = self.endpoints[index]
            for auth_attempt in range(2):
                token = self.credential.get() if self.credential else self.token
                request = urllib.request.Request(endpoint + '/v3/' + path, data,
                    {'Content-Type':'application/json', **({'Authorization':token} if token else {})})
                try:
                    with self.opener.open(request, timeout=self.timeout) as response:
                        raw = response.read(4 * 1024**2 + 1)
                    if len(raw) > 4 * 1024**2:
                        raise CoordinationUnavailable('Oversized etcd response')
                    result = json.loads(raw)
                    if 'error' in result or 'code' in result:
                        raise CoordinationUnavailable('etcd rejected coordination request')
                    with self.endpoint_lock:
                        self.preferred = index
                    return result
                except urllib.error.HTTPError as exc:
                    if exc.code in (400,401) and self.credential and auth_attempt == 0:
                        try:
                            error=json.loads(exc.read(65536))
                            rejected = ((exc.code==401 and error.get('code')==16) or
                                (exc.code==400 and error.get('code')==3 and
                                 error.get('message')=='etcdserver: revision of auth store is old'))
                        except (ValueError,AttributeError):
                            rejected = False
                        if rejected:
                            # Both exact etcd auth rejections precede effects:
                            # invalid token or stale auth-store revision. Other
                            # code-3 validation errors, denied permissions and
                            # unknown replies never use this resend path.
                            self.credential.get(rejected=token)
                            continue
                    if exc.code not in (429, 500, 502, 503, 504):
                        raise CoordinationUnavailable('etcd authorization or request rejected') from None
                    if not retry_unknown:raise CoordinationUnavailable('etcd administration reply unknown; inspect before retrying') from None
                    break
                except (urllib.error.URLError, OSError, http.client.HTTPException, json.JSONDecodeError):
                    if not retry_unknown:raise CoordinationUnavailable('etcd administration outcome unknown; mutation not retried') from None
                    break
        # Repeating a conditional transaction can report false after a lost
        # successful response. Callers always reconcile by reading identities.
        raise CoordinationUnavailable('No etcd quorum response; outcome may be unknown')

    def _key(self, key):
        if not isinstance(key, str) or not key or key.startswith('/') or '..' in key.split('/'):
            raise ValueError('Invalid coordination key')
        return encode(self.prefix + key)

    def read(self, key):
        result = self._rpc('kv/range', {'key':self._key(key), 'serializable':False})
        values = result.get('kvs', [])
        if not values:
            return Record(key, None, 0)
        item = values[0]
        value = json.loads(base64.b64decode(item['value'], validate=True))
        if not isinstance(value, dict):
            raise CoordinationUnavailable('Malformed coordination record')
        return Record(key, value, int(item['mod_revision']))

    def scan(self, prefix):
        """Bounded, revision-pinned inventory for trusted reconciliation."""
        raw = (self.prefix + prefix).encode()
        if not raw or raw[-1] >= 127:
            raise ValueError('ASCII coordination prefix required')
        end = base64.b64encode(raw[:-1] + bytes([raw[-1]+1])).decode()
        start, revision, records = self._key(prefix), None, []
        while True:
            payload = dict(key=start, range_end=end, limit='128', sort_order='ASCEND', sort_target='KEY')
            if revision is not None:
                payload['revision'] = str(revision)
            result = self._rpc('kv/range', payload)
            if revision is None:
                revision = int(result['header']['revision'])
            items = result.get('kvs', [])
            for item in items:
                full_key = base64.b64decode(item['key'], validate=True).decode()
                value = json.loads(base64.b64decode(item['value'], validate=True))
                if not full_key.startswith(self.prefix) or not isinstance(value, dict):
                    raise CoordinationUnavailable('Malformed coordination inventory')
                records.append(Record(full_key[len(self.prefix):], value, int(item['mod_revision'])))
            if len(records) > 4096:
                raise CoordinationUnavailable('Inventory exceeds configured reconciliation bound')
            if not result.get('more'):
                return records
            if not items:
                raise CoordinationUnavailable('Incomplete coordination inventory')
            start = base64.b64encode(base64.b64decode(items[-1]['key']) + b'\x00').decode()

    def transact(self, reads, writes):
        if len({r.key for r in reads}) != len(reads):
            raise ValueError('Duplicate transaction reads')
        if not set(writes).issubset({r.key for r in reads}):
            raise ValueError('Every changed key must have a compare guard')
        compare = [dict(key=self._key(r.key), target='MOD', result='EQUAL',
                        modRevision=str(r.revision)) for r in reads]
        success = []
        for key, value in writes.items():
            if not isinstance(value, dict):
                raise ValueError('Coordination writes must be JSON objects')
            raw = json.dumps(value, sort_keys=True, separators=(',', ':'))
            if len(raw.encode()) > 1024**2:
                raise ValueError('Coordination record too large')
            success.append({'requestPut':{'key':self._key(key), 'value':encode(raw)}})
        return bool(self._rpc('kv/txn', {'compare':compare, 'success':success,
                                        'failure':[]}).get('succeeded', False))
