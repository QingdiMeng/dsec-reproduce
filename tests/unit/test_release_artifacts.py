"""Offline release identity, bad artifacts and partial staging remain explicit."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from dsec.contracts.release import COMPATIBILITY, coherent_release
from dsec.host.release_artifacts import inspect_wheel
from dsec.host.releases import stage


class ReleaseArtifactTests(unittest.TestCase):
    def wheel(self,root,version='0.1.0.dev0',contract=None):
        wheel=root/f'dsec_reproduce-{version}-py3-none-any.whl'
        with ZipFile(wheel,'w') as archive:
            archive.writestr(f'dsec_reproduce-{version}.dist-info/METADATA',f'Name: dsec-reproduce\nVersion: {version}\n')
            archive.writestr('dsec/contracts/release.py','COMPATIBILITY = '+repr(contract or COMPATIBILITY)+'\n')
            archive.writestr('dsec/__init__.py','# fixture\n')
        return wheel,hashlib.sha256(wheel.read_bytes()).hexdigest()

    def test_build_distinguishes_versions_and_tampering_rejects_before_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);a,sha=self.wheel(root);b,sha2=self.wheel(root,'0.1.0.dev1')
            self.assertNotEqual(inspect_wheel(a,sha)['build'],inspect_wheel(b,sha2)['build'])
            a.write_bytes(a.read_bytes()+b'tampered')
            with self.assertRaises(ValueError):stage(a,sha,root/'releases')
            self.assertFalse((root/'releases').exists())

    def test_inspection_uses_the_hashed_bytes_if_the_input_path_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);a,sha=self.wheel(root);b,_=self.wheel(root,'0.1.0.dev1')
            replacement=b.read_bytes();read=Path.read_bytes
            def swap(path):
                data=read(path)
                if path==a:path.write_bytes(replacement)
                return data
            with patch.object(Path,'read_bytes',swap):ref=inspect_wheel(a,sha)
            self.assertEqual(ref['version'],'0.1.0.dev0')

    def test_partial_install_is_never_published_or_reused_as_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);wheel,sha=self.wheel(root);release=inspect_wheel(wheel,sha)
            stable=root/'releases/stable';stable.mkdir(parents=True);(stable/'kept').write_text('old')
            (root/'releases').chmod(0o700)
            with patch('dsec.host.releases.venv.EnvBuilder') as builder:
                builder.return_value.create.side_effect=RuntimeError('interrupted')
                with self.assertRaises(RuntimeError):stage(wheel,sha,root/'releases')
            self.assertFalse((root/'releases'/release['build']/'release.json').exists())
            with self.assertRaises(FileNotFoundError):stage(wheel,sha,root/'releases')
            self.assertEqual((stable/'kept').read_text(),'old')

    def test_mixed_roles_and_missing_fingerprint_are_not_coherent(self):
        ref=dict(build='a'*64,version='0.1.0',compatibility=COMPATIBILITY)
        with self.assertRaises(ValueError):coherent_release(dict(edge=ref,episode=None))
        with self.assertRaises(ValueError):coherent_release(dict(edge=ref,gateway=dict(ref,build='b'*64)))


if __name__=='__main__':unittest.main()
