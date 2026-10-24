"""Operator rotation retains the only replacement secret before a protected send."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dsec.host.credentials import prepare_api


class SecurityCLITests(unittest.TestCase):
    def test_lost_prepare_reply_keeps_replacement_and_original_rotation_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);client=root/'client.json';out=root/'replacement'
            token=root/'old-token';token.write_text('a'*64);token.chmod(0o600)
            client.write_text(json.dumps(dict(endpoints=['https://api.example:443'],project='root',token_file=str(token))))
            def send(operation,**args):
                intent=json.loads((out/'intent.json').read_text())
                self.assertEqual(intent['rotation_id'],args['rotation_id'])
                self.assertEqual((out/'token').read_text().strip(),args['new_token'])
                self.assertEqual((out/'token').stat().st_mode & 0o777,0o600)
                raise TimeoutError('lost response')
            with patch('dsec.host.credentials.ClusterTransport') as transport:
                transport.return_value.call.side_effect=send
                with self.assertRaises(TimeoutError):prepare_api(client,out)
                transport.return_value.call.assert_called_once()
                with self.assertRaises(FileExistsError):prepare_api(client,out)
                self.assertEqual(transport.return_value.call.call_count,1)
            self.assertEqual(json.loads((out/'intent.json').read_text())['state'],'UNCONFIRMED')
            self.assertNotIn((out/'token').read_text().strip(),(out/'intent.json').read_text())


if __name__=='__main__':unittest.main()
