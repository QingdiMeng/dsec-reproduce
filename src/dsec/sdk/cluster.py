"""TLS cluster transport reusing the native SDK, without automatic mutation replay."""
from collections import OrderedDict
import json
import http.client
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid

from dsec.contracts.cluster import identifier, request_id as checked_id
from dsec.contracts.errors import RequestOutcomeUnknown
from dsec.sdk.sandbox_transport import ServiceError


class JSONRPC:
    def __init__(self, endpoints, *, ca_file=None, cert_file=None, key_file=None,
                 allow_loopback_http=False):
        self.endpoints = tuple(url.rstrip('/') for url in endpoints)
        if not self.endpoints:
            raise ValueError('Explicit cluster endpoints required')
        for url in self.endpoints:
            parsed = urllib.parse.urlsplit(url)
            if (not parsed.hostname or parsed.username or parsed.password or
                    parsed.path or parsed.query or parsed.fragment):
                raise ValueError('Invalid cluster endpoint')
            if parsed.scheme != 'https' and not (allow_loopback_http and parsed.scheme == 'http'
                    and parsed.hostname in ('localhost', '127.0.0.1', '::1')):
                raise ValueError('Verified TLS required for remote endpoints')
        context = ssl.create_default_context(cafile=ca_file)
        if bool(cert_file) != bool(key_file):
            raise ValueError('Both client certificate and key required')
        if cert_file:
            context.load_cert_chain(cert_file, key_file)
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context), NoRedirect())

    def send(self, token, payload, *, readonly=False, timeout=330):
        if not isinstance(token, str) or '\n' in token or '\r' in token:
            raise ValueError('Invalid authentication credential')
        data = json.dumps(payload, separators=(',', ':')).encode()
        if len(data) > 131072:
            raise ValueError('Cluster request too large')
        for endpoint in self.endpoints:
            request = urllib.request.Request(endpoint + '/v1/call', data,
                {'Content-Type':'application/json', 'Authorization':'Bearer ' + token})
            try:
                with self.opener.open(request, timeout=timeout) as response:
                    raw = response.read(8*1024**2+1)
                if len(raw) > 8*1024**2:
                    raise ValueError('Oversized cluster response')
                result = json.loads(raw)
                if result.get('request_id') != payload.get('request_id'):
                    raise ValueError('Cluster response ID mismatch')
                if not result['ok']:
                    error = result['error']
                    if error['type'] == 'RequestOutcomeUnknown':
                        raise RequestOutcomeUnknown(error['message'], payload.get('request_id'))
                    raise ServiceError(error['type'], error['message'], error.get('details'))
                return result['result']
            except urllib.error.HTTPError as exc:
                # A protected mutation may have been accepted even if a proxy
                # returned 5xx. Never select another endpoint to resend it.
                if readonly and exc.code in (502, 503, 504):
                    continue
                raise RequestOutcomeUnknown('Cluster HTTP response lost; reconcile by ID', payload.get('request_id')) from exc
            except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
                if readonly:
                    continue
                raise RequestOutcomeUnknown('Cluster transport failed; mutation not retried', payload.get('request_id')) from exc
        raise RequestOutcomeUnknown('No cluster endpoint available', payload.get('request_id'))


class ClusterTransport:
    def __init__(self, endpoints, *, token, project, **tls):
        self.rpc, self.token, self.project = JSONRPC(endpoints, **tls), token, identifier(project)
        self.lock, self.request_routes = threading.Lock(), OrderedDict()

    def call(self, operation, sandbox_id=None, *, request_id=None, resource_demand=None, **args):
        operation_id = checked_id(request_id or uuid.uuid4().hex)
        if sandbox_id is not None:
            with self.lock:
                self.request_routes[operation_id] = sandbox_id
                self.request_routes.move_to_end(operation_id)
                if len(self.request_routes) > 4096:
                    self.request_routes.popitem(last=False)
        envelope = dict(project=self.project, request_id=operation_id, operation=operation,
                        sandbox_id=sandbox_id, args=args)
        if resource_demand is not None:
            envelope['resource_demand'] = resource_demand
        readonly = operation in ('project.get', 'project.list', 'admission.list', 'sandbox.list',
            'audit.list', 'node.list', 'health', 'node_status', 'status', 'container_status',
            'query_request', 'container_query_request', 'container_query_action', 'native_events') or (
            operation == 'native' and args.get('action') in ('read', 'capabilities'))
        return self.rpc.send(self.token, envelope, readonly=readonly)

    def query_request(self, operation_id, *, sandbox_id=None):
        checked_id(operation_id)
        with self.lock:
            sandbox_id = sandbox_id or self.request_routes.get(operation_id)
        return self.call('query_request', sandbox_id, lookup_id=operation_id)


# Compatibility name for the API client. There is one public implementation.
from dsec.sdk.client import DSecClient as ClusterClient
