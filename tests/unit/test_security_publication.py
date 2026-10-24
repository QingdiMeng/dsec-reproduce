"""Private generation CAS and live credential selection retain old route IDs."""
import json
from pathlib import Path
import tempfile
import threading
import unittest

from dsec._credentials import NodeCredentials, FileCredential
from dsec._persistence import atomic_json
from dsec.contracts.cluster import SandboxRoute
from dsec.host import security_publication


class SecurityPublicationTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name);self.path=self.root/'auth.json'
    def doc(self,generation=1,active='a',keys=None,legacy=False):
        return dict(generation=generation,active=active,keys=keys or {'a':'a'*64},allow_legacy=legacy)

    def test_node_overlap_cutover_and_retirement_do_not_change_existing_route_integrity(self):
        old_route=SandboxRoute('root','n1','microvm','1'*12,'2'*32);key=b'r'*32;encoded=old_route.encode(key)
        security_publication.publish(self.path,self.doc(),0,'node-auth');source=NodeCredentials(self.path,'n1');old=source()
        security_publication.publish(self.path,self.doc(2,'b',{'a':'a'*64,'b':'b'*64}),1,'node-auth')
        self.assertTrue(source.accepts(old));self.assertNotEqual(source(),old)
        security_publication.publish(self.path,self.doc(3,'b',{'b':'b'*64}),2,'node-auth');self.assertFalse(source.accepts(old))
        self.assertEqual(SandboxRoute.decode(encoded,key),old_route)
        atomic_json(self.path,self.doc(1))
        with self.assertRaises(ValueError):source()

    def test_competing_publishers_cannot_overwrite_the_winning_generation(self):
        security_publication.publish(self.path,self.doc(),0,'node-auth');barrier=threading.Barrier(3);results=[]
        def run(active):
            barrier.wait()
            try:results.append(security_publication.publish(self.path,self.doc(2,active,{active:active*64}),1,'node-auth'))
            except ValueError:results.append('conflict')
        threads=[threading.Thread(target=run,args=(key,)) for key in ('b','c')]
        for t in threads:t.start()
        barrier.wait()
        for t in threads:t.join(5);self.assertFalse(t.is_alive())
        self.assertEqual(results.count('conflict'),1)
        self.assertEqual(json.loads(self.path.read_text())['generation'],2)

    def test_bad_publication_and_unsafe_new_token_fail_closed(self):
        security_publication.publish(self.path,self.doc(),0,'node-auth');before=self.path.read_bytes()
        with self.assertRaises(ValueError):security_publication.publish(self.path,self.doc(2,'missing'),1,'node-auth')
        self.assertEqual(self.path.read_bytes(),before)
        token=self.root/'token';token.write_text('a'*64);token.chmod(0o600);provider=FileCredential(token)
        token.write_text('b'*64);self.assertEqual(provider(),'b'*64)
        token.chmod(0o644)
        with self.assertRaises(ValueError):provider()

    def test_container_material_mapping_preserves_references_and_rejects_escape(self):
        material=self.root/'private';material.mkdir(mode=0o700)
        password=material/'password';password.write_text('a'*64);password.chmod(0o600)
        value=dict(generation=1,username='dsec-r2',password_file='/secrets/password')
        options=dict(reference_prefix=Path('/secrets'),material_directory=material)
        security_publication.publish(self.path,value,0,'etcd-identity',**options)
        self.assertEqual(json.loads(self.path.read_text()),value)
        for target in ('/other/password','/secrets/../password','/secrets/nested/password'):
            with self.assertRaises(ValueError):
                security_publication.publish(self.path,dict(value,generation=2,password_file=target),1,'etcd-identity',**options)
        password.unlink();password.symlink_to(self.root/'external-password')
        (self.root/'external-password').write_text('b'*64)
        with self.assertRaises(OSError):
            security_publication.publish(self.path,dict(value,generation=2),1,'etcd-identity',**options)
        self.assertEqual(json.loads(self.path.read_text()),value)


if __name__=='__main__':unittest.main()
