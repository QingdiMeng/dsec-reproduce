"""Controlled IAM rotation races, not a second identity implementation."""
import threading
import unittest

from dsec.contracts.cluster import AuthorizationDenied, CoordinationUnavailable, Quota
from dsec.contracts.requests import RequestConflict
from dsec.control.iam import IAM, credential_key
from dsec.control import credential_rotation
from tests.unit.test_cluster_control import MemoryStore


class CredentialRotationTests(unittest.TestCase):
    old='a'*64;new='b'*64;identity='1'*32
    def setUp(self):
        self.store=MemoryStore();self.iam=IAM(self.store);self.iam.bootstrap(self.old,Quota(2,2,2048,4096,10))
    def prepare(self):return credential_rotation.rotate(self.iam,self.old,self.identity,'prepare',new_token=self.new)
    def authorize(self,token):return self.iam.authorize(token,'root','sandbox.read')

    def test_overlap_and_completion_prove_the_new_token_without_storing_secrets(self):
        first=self.prepare();self.assertEqual(first,self.prepare())
        self.authorize(self.old);self.authorize(self.new)
        with self.assertRaises(AuthorizationDenied):credential_rotation.rotate(self.iam,self.old,self.identity,'complete')
        done=credential_rotation.rotate(self.iam,self.new,self.identity,'complete');self.assertEqual(done['state'],'COMPLETED')
        with self.assertRaises(AuthorizationDenied):self.authorize(self.old)
        self.authorize(self.new);self.assertEqual(done,credential_rotation.rotate(self.iam,self.new,self.identity,'complete'))
        with self.assertRaises((AuthorizationDenied,RequestConflict)):credential_rotation.rotate(self.iam,self.new,self.identity,'cancel')
        self.authorize(self.new)
        raw=repr(self.store.records);self.assertNotIn(self.old,raw);self.assertNotIn(self.new,raw)

    def test_cancel_keeps_source_and_delayed_completion_cannot_revoke_it(self):
        self.prepare();self.assertEqual(credential_rotation.rotate(self.iam,self.old,self.identity,'cancel')['state'],'CANCELLED')
        self.authorize(self.old)
        with self.assertRaises(AuthorizationDenied):credential_rotation.rotate(self.iam,self.new,self.identity,'complete')
        with self.assertRaises(RequestConflict):credential_rotation.rotate(self.iam,self.old,'2'*32,'prepare',new_token=self.new)

    def test_completed_rotation_cannot_be_cancelled_by_delayed_credentials(self):
        self.prepare();credential_rotation.rotate(self.iam,self.new,self.identity,'complete')
        with self.assertRaises((AuthorizationDenied,RequestConflict)):credential_rotation.rotate(self.iam,self.new,self.identity,'cancel')
        self.authorize(self.new)

    def test_complete_and_cancel_share_the_same_atomic_authorization_boundary(self):
        self.prepare();entered=threading.Event();release=threading.Event();errors=[]
        def before(reads,writes):
            r=writes.get('credential-rotations/'+self.identity)
            if r and r['state']=='CANCELLED':entered.set();self.assertTrue(release.wait(5))
        self.store.before_commit=before
        def cancel():
            try:credential_rotation.rotate(self.iam,self.old,self.identity,'cancel')
            except Exception as exc:errors.append(exc)
        thread=threading.Thread(target=cancel);thread.start()
        try:
            self.assertTrue(entered.wait(5));credential_rotation.rotate(self.iam,self.new,self.identity,'complete')
        finally:release.set();thread.join(5)
        self.assertFalse(thread.is_alive());self.assertEqual(len(errors),1)
        self.assertIsInstance(errors[0],AuthorizationDenied);self.authorize(self.new)

    def test_unknown_quorum_never_acknowledges_retirement_or_uses_local_fallback(self):
        self.prepare();self.store.unavailable=True
        with self.assertRaises(CoordinationUnavailable):credential_rotation.rotate(self.iam,self.new,self.identity,'complete')
        self.store.unavailable=False;self.authorize(self.old);self.authorize(self.new)
        self.assertEqual(credential_rotation.rotate(self.iam,self.new,self.identity,'get')['state'],'OVERLAP')


if __name__=='__main__':unittest.main()
