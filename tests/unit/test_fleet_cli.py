"""Operator CLI retains identity before API sends; no local host provisioning."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dsec.host import fleet
from dsec.contracts.release import COMPATIBILITY


class FleetCLITests(unittest.TestCase):
    def test_upgrade_passes_the_verified_reference_and_expected_membership_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);client=root/'client.json';out=root/'intent.json';release=root/'release.json'
            client.write_text(json.dumps(dict(endpoints=['https://api.example:443'],token_file='token')))
            reference=dict(build='a'*64,version='0.1.dev1',compatibility=COMPATIBILITY)
            release.write_text(json.dumps(reference))
            argv=['fleet','--client',str(client),'--out',str(out),'upgrade','--node','n1','--version','4','--release',str(release)]
            with patch('sys.argv',argv),patch.object(fleet,'secret_file',return_value='t'*64), \
                 patch.object(fleet,'ClusterTransport') as transport,redirect_stdout(io.StringIO()):
                transport.return_value.call.return_value={'state':'MAINTENANCE','version':5}
                fleet.main()
            saved=json.loads(out.read_text())
            self.assertEqual(saved['args'],dict(node='n1',version=4,release=reference))
            self.assertEqual(transport.return_value.call.call_args.args,('node.upgrade',))

    def test_mutation_persists_identity_before_send_and_never_replaces_result(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);client=root/'client.json';out=root/'drain.json'
            client.write_text(json.dumps(dict(endpoints=['https://api.example:443'],token_file='token',ca_file='ca.crt')))
            def send(operation,**args):
                intent=json.loads(out.read_text())
                self.assertEqual(intent['state'],'PENDING')
                self.assertEqual(intent['request_id'],args['request_id'])
                self.assertEqual(out.stat().st_mode & 0o777,0o600)
                return dict(state='DRAINING',version=3)
            argv=['fleet','--client',str(client),'--out',str(out),'drain','--node','n1','--version','2']
            with patch('sys.argv',argv),patch.object(fleet,'secret_file',return_value='t'*64), \
                 patch.object(fleet,'ClusterTransport') as transport,redirect_stdout(io.StringIO()):
                transport.return_value.call.side_effect=send
                fleet.main()
                self.assertEqual(json.loads(out.read_text())['state'],'DONE')
                transport.return_value.call.reset_mock()
                with self.assertRaises(FileExistsError):fleet.main()
                transport.return_value.call.assert_not_called()

    def test_lost_reply_retains_original_intent_without_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);client=root/'client.json';out=root/'intent.json'
            client.write_text(json.dumps(dict(endpoints=['https://api.example:443'],token_file='token')))
            argv=['fleet','--client',str(client),'--out',str(out),'--request-id','a'*32,'retire','--node','n1','--version','3']
            with patch('sys.argv',argv),patch.object(fleet,'secret_file',return_value='t'*64), \
                 patch.object(fleet,'ClusterTransport') as transport,redirect_stdout(io.StringIO()):
                transport.return_value.call.side_effect=TimeoutError('lost reply')
                with self.assertRaises(TimeoutError):fleet.main()
                transport.return_value.call.assert_called_once()
            saved=json.loads(out.read_text())
            self.assertEqual(saved['request_id'],'a'*32)
            self.assertEqual(saved['state'],'UNCONFIRMED')
            self.assertEqual(saved['args'],dict(node='n1',version=3))
