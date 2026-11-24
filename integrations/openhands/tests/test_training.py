import unittest
import tempfile
from pathlib import Path
import json
from dsec_openhands.training import SampledGeneration, episode_actions


class TrainingContractTests(unittest.TestCase):
    def test_native_identity_and_logprob_alignment(self):
        event = dict(obj=dict(input_ids=[1,2]),out=dict(output_ids=[3,4],
            meta_info=dict(id='a',completion_tokens=2,output_token_logprobs=[[-.1,3,None],[-.2,4,None]])))
        response = dict(id='a',choices=[dict(response_token_ids=[3,4])])
        record=SampledGeneration.from_sglang(event,response,policy_version=2,purpose='agent')
        self.assertEqual(record.behavior_logprobs,[-.1,-.2])
        event['out']['meta_info']['output_token_logprobs'][0][1]=99
        with self.assertRaises(ValueError):
            SampledGeneration.from_sglang(event,response,policy_version=2,purpose='agent')

    def test_nonprefix_contexts_preserved_and_summary_excluded(self):
        a=SampledGeneration('a',0,[1,2],[3],[-.2],'agent')
        summary=SampledGeneration('s',0,[9,8],[7],[-.4],'summary')
        b=SampledGeneration('b',1,[1,4,5],[6],[-.3],'agent')
        selected=episode_actions([a,summary,b],['a','s','b'])
        self.assertEqual([x.response_id for x in selected],['a','b'])
        self.assertEqual(selected[1].prompt_ids,[1,4,5])
        self.assertEqual(selected[1].policy_version,1)

    def test_incomplete_native_log_record_is_retained_until_complete(self):
        from dsec_openhands.sglang_training import NativeRecords
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'native.log'
            event=dict(event='request.finished',out=dict(meta_info=dict(id='a')))
            line=json.dumps(event)
            path.write_text(line[:20])
            reader=NativeRecords(directory)
            with self.assertRaises(TimeoutError):
                reader.get('a',seconds=0)
            with path.open('a') as out:
                out.write(line[20:]+'\n')
            self.assertEqual(reader.get('a',seconds=0),event)

    def test_rotated_native_log_uses_the_new_files_position(self):
        from dsec_openhands.sglang_training import NativeRecords
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'native.log'
            event=lambda name:dict(event='request.finished',out=dict(meta_info=dict(id=name)))
            first=event('a-long-first-response-id')
            path.write_text(json.dumps(first)+'\n')
            reader=NativeRecords(directory)
            self.assertEqual(reader.get(first['out']['meta_info']['id'],seconds=0),first)
            path.rename(path.with_suffix('.log.previous'))
            second=event('b')
            path.write_text(json.dumps(second)+'\n')
            self.assertEqual(reader.get('b',seconds=0),second)
            self.assertNotIn(first['out']['meta_info']['id'],reader.records)

    def test_copy_truncated_native_log_restarts_at_zero(self):
        from dsec_openhands.sglang_training import NativeRecords
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'native.log'
            first=dict(event='request.finished',out=dict(meta_info=dict(id='a-long-first-response-id')))
            path.write_text(json.dumps(first)+'\n')
            reader=NativeRecords(directory)
            reader.get(first['out']['meta_info']['id'],seconds=0)
            second=dict(event='request.finished',out=dict(meta_info=dict(id='b')))
            path.write_text(json.dumps(second)+'\n')
            self.assertEqual(reader.get('b',seconds=0),second)


if __name__=='__main__':
    unittest.main()
