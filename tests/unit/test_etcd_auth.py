"""Controlled clocks/HTTP boundaries; fake JWTs are expiry hints, not signatures."""
import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error

from dsec.contracts.cluster import CoordinationUnavailable
from dsec.control.coordination import EtcdStore
from dsec.control.etcd_auth import EtcdCredential


def jwt(exp,marker='test'):
    body=base64.urlsafe_b64encode(json.dumps({'exp':exp,'marker':marker}).encode()).decode().rstrip('=')
    return 'header.'+body+'.'+'s'*48


class Reply(io.BytesIO):
    def __init__(self,value):super().__init__(json.dumps(value).encode())


class CredentialTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup)
        self.password=Path(t.name)/'password';self.password.write_text('p'*64);self.password.chmod(0o600)

    def provider(self,opener,**kwargs):
        return EtcdCredential(['https://etcd.example:2379'],opener,username='dsec',
            password_file=self.password,renew_before_seconds=10,**kwargs)

    def test_concurrent_renewal_logs_in_once_and_rejected_old_token_cannot_invalidate_new(self):
        class Opener:
            calls=0
            def open(self,request,timeout):
                self.calls+=1
                self.request=request
                return Reply({'token':jwt(1100,'new')})
        o=Opener();old=jwt(999,'expired')
        with patch('dsec.control.etcd_auth.time.time',return_value=1000):
            c=self.provider(o,token=old)
            with ThreadPoolExecutor(max_workers=8) as pool:
                values=list(pool.map(lambda _:c.get(),range(16)))
            self.assertEqual(o.calls,1);self.assertEqual(set(values),{jwt(1100,'new')})
            self.assertEqual(c.get(rejected=old),values[0]);self.assertEqual(o.calls,1)
        self.assertNotIn('Authorization',o.request.headers)
        self.assertEqual(json.loads(o.request.data)['name'],'dsec')

    def test_failed_early_renewal_uses_unexpired_token_but_expiry_and_rejection_fail_closed(self):
        class Offline:
            calls=0
            def open(self,*args,**kwargs):
                self.calls+=1;raise urllib.error.URLError('offline')
        o=Offline();old=jwt(1005)
        with patch('dsec.control.etcd_auth.time.time',return_value=1000):
            c=self.provider(o,token=old)
            self.assertEqual(c.get(),old);self.assertEqual(c.get(),old);self.assertEqual(o.calls,1)
            with self.assertRaises(CoordinationUnavailable):c.get(rejected=old)
        with patch('dsec.control.etcd_auth.time.time',return_value=1006):
            with self.assertRaises(CoordinationUnavailable):c.get()
        self.assertEqual(o.calls,2)

    def test_bad_password_or_permission_cannot_fall_back_after_rejection(self):
        class Denied:
            def open(self,*args,**kwargs):
                raise urllib.error.HTTPError('https://etcd.example',400,'denied',{},io.BytesIO(b'private diagnostic'))
        with patch('dsec.control.etcd_auth.time.time',return_value=1000):
            c=self.provider(Denied(),token=jwt(2000))
            with self.assertRaises(CoordinationUnavailable) as raised:c.get(rejected=c.token)
            self.assertNotIn('private diagnostic',str(raised.exception))
        self.password.chmod(0o644)
        with self.assertRaises(CoordinationUnavailable):self.provider(Denied()).get()
        with self.assertRaises(ValueError):
            EtcdCredential(['http://127.0.0.1:2379'],Denied(),username='dsec',password_file=self.password)

    def test_known_unauthenticated_rejection_refreshes_once_but_forbidden_and_lost_reply_do_not(self):
        class Opener:
            def __init__(self,status):self.status=status;self.login=0;self.kv=0
            def open(self,request,timeout):
                if request.full_url.endswith('authenticate'):
                    self.login+=1;return Reply({'token':jwt(2000,'new')})
                self.kv+=1
                if self.kv==1:
                    raise urllib.error.HTTPError(request.full_url,self.status,'rejected',{},
                        io.BytesIO(json.dumps({'code':16 if self.status==401 else 7}).encode()))
                return Reply({'kvs':[]})
        with patch('dsec.control.etcd_auth.time.time',return_value=1000):
            for status in (401,403,500):
                store=EtcdStore(['https://etcd.example:2379'],token=jwt(2000),
                    authentication={'username':'dsec','password_file':str(self.password)})
                o=Opener(status);store.opener=o;store.credential.opener=o
                if status==401:
                    self.assertIsNone(store.read('object').value)
                    self.assertEqual((o.login,o.kv),(1,2))
                else:
                    with self.assertRaises(CoordinationUnavailable):store.read('object')
                    self.assertEqual((o.login,o.kv),(0,1))


if __name__=='__main__':unittest.main()
