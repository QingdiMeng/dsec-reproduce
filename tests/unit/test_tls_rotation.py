"""Real TLS handshakes with controlled trust cutover; no VM/HA claims."""
import hashlib
import http.client
import json
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
import uuid

from deploy.cluster.pki import certificate
from dsec._credentials import NodeCredentials
from dsec.host.cluster_http import ClusterHTTPServer
from dsec.host.security_publication import publish
from dsec.sdk.cluster import JSONRPC
from dsec.sdk.sandbox_transport import ServiceError
from dsec.contracts.errors import RequestOutcomeUnknown


@unittest.skipUnless(shutil.which('openssl'),'TLS rotation fixture requires OpenSSL')
class TLSRotationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)
        cls.material={}
        for label in ('old','new'):
            p=cls.root/label;p.mkdir()
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(p/'ca.key'),
                '-out',str(p/'ca.crt'),'-days','1','-subj','/CN='+label+'-rotation-CA',
                '-addext','basicConstraints=critical,CA:TRUE','-addext','keyUsage=critical,keyCertSign,cRLSign',
                '-addext','subjectKeyIdentifier=hash'],check=True,capture_output=True)
            for role,usage in (('server','serverAuth'),('client','clientAuth')):
                cert,key=certificate(p/'ca.crt',p/'ca.key',p/role,role,('localhost','127.0.0.1'),usage)
                key.chmod(0o600)
                der=ssl.PEM_cert_to_DER_cert(cert.read_text())
                cls.material[label+'-'+role]=dict(cert_file=str(cert),key_file=str(key),fingerprint=hashlib.sha256(der).hexdigest())
        (cls.root/'both.pem').write_bytes((cls.root/'old/ca.crt').read_bytes()+(cls.root/'new/ca.crt').read_bytes())

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.work=Path(temp.name)
        self.server_path=self.work/'server.json';self.client_path=self.work/'client.json';self.keys=self.work/'keys.json'
        self.calls=[]
        publish(self.keys,dict(generation=1,active='a',keys={'a':'a'*64},allow_legacy=False),0,'node-auth')
        self.auth=NodeCredentials(self.keys,'n1');self.old_token=self.auth()
        publish(self.server_path,self.server_doc(1,'old','old'),0,'tls-server')
        publish(self.client_path,self.client_doc(1,'old','old'),0,'tls-client')
        def dispatch(token,payload):self.calls.append(payload);return {'effect':len(self.calls)}
        server=ClusterHTTPServer(('127.0.0.1',0),dispatch,security_file=str(self.server_path),node_token=NodeCredentials(self.keys,'n1'))
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.02});thread.start()
        def stop():server.shutdown();thread.join(5);server.server_close()
        self.addCleanup(stop);self.server=server
        self.rpc=JSONRPC([f'https://localhost:{server.server_port}'],security_file=str(self.client_path))

    def server_doc(self,generation,leaf,trust,peers=None):
        mat=self.material[leaf+'-server']
        ca=self.root/('both.pem' if trust=='both' else trust+'/ca.crt')
        return dict(generation=generation,cert_file=mat['cert_file'],key_file=mat['key_file'],client_ca_file=str(ca),
                    peer_sha256=peers or [self.material[leaf+'-client']['fingerprint']])

    def client_doc(self,generation,leaf,trust,peers=None):
        mat=self.material[leaf+'-client'];ca=self.root/('both.pem' if trust=='both' else trust+'/ca.crt')
        return dict(generation=generation,cert_file=mat['cert_file'],key_file=mat['key_file'],ca_file=str(ca),
                    peer_sha256=peers or [self.material[leaf+'-server']['fingerprint']])

    def send(self,token=None):return self.rpc.send(token or self.auth,dict(request_id=uuid.uuid4().hex,operation='effect'))

    def test_existing_server_client_and_node_key_providers_rotate_without_restart(self):
        self.assertEqual(self.send()['effect'],1);port=self.server.server_port
        clients=[self.material[k+'-client']['fingerprint'] for k in ('old','new')]
        servers=[self.material[k+'-server']['fingerprint'] for k in ('old','new')]
        publish(self.server_path,self.server_doc(2,'old','both',clients),1,'tls-server')
        publish(self.client_path,self.client_doc(2,'new','both',servers),1,'tls-client')
        self.send()
        publish(self.server_path,self.server_doc(3,'new','both',clients),2,'tls-server');self.send()
        publish(self.keys,dict(generation=2,active='b',keys={'a':'a'*64,'b':'b'*64},allow_legacy=False),1,'node-auth')
        self.send();self.send(self.old_token)
        publish(self.keys,dict(generation=3,active='b',keys={'b':'b'*64},allow_legacy=False),2,'node-auth')
        count=len(self.calls)
        with self.assertRaises(ServiceError):self.send(self.old_token)
        self.assertEqual(len(self.calls),count)
        publish(self.client_path,self.client_doc(3,'new','new'),2,'tls-client')
        publish(self.server_path,self.server_doc(4,'new','new'),3,'tls-server');self.send()
        self.assertEqual(port,self.server.server_port)
        bad=self.server_doc(5,'new','new');bad['key_file']=self.material['old-server']['key_file']
        with self.assertRaises(ssl.SSLError):publish(self.server_path,bad,4,'tls-server')
        self.send()

    def test_connection_authenticated_before_retirement_cannot_submit_a_new_effect_after_it(self):
        context=ssl.create_default_context(cafile=str(self.root/'old/ca.crt'))
        context.load_cert_chain(self.material['old-client']['cert_file'],self.material['old-client']['key_file'])
        conn=http.client.HTTPSConnection('localhost',self.server.server_port,context=context,timeout=5)
        self.addCleanup(conn.close);conn.connect()  # TLS accepted, no application request yet.
        publish(self.server_path,self.server_doc(2,'new','new'),1,'tls-server')
        payload=json.dumps(dict(request_id=uuid.uuid4().hex,operation='effect'))
        conn.request('POST','/v1/call',payload,{'Authorization':'Bearer '+self.old_token,'Content-Type':'application/json'})
        response=json.loads(conn.getresponse().read())
        self.assertFalse(response['ok']);self.assertEqual(self.calls,[])


if __name__=='__main__':unittest.main()
