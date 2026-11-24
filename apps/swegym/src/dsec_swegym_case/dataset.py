"""Prepare a pinned training subset while separating model input from answers."""
import argparse
import hashlib
import json
from pathlib import Path
import re

SOURCE_ID='SWE-Gym/SWE-Gym-Lite'
SOURCE_SHA256='f3a7cd934e8cc523b6053298d0abb2c82fd7db2b83f9f2ccba5944545aaa4eb1'
SOURCE_URL='https://huggingface.co/datasets/SWE-Gym/SWE-Gym-Lite/resolve/main/data/train-00000-of-00001.parquet'
REQUIRED={'instance_id','repo','version','base_commit','problem_statement','hints_text','patch','test_patch','FAIL_TO_PASS','PASS_TO_PASS'}


def validate_task(value):
    if not isinstance(value,dict) or not REQUIRED<=set(value):raise ValueError('Incomplete SWE-Gym row')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+',value['instance_id']):raise ValueError('Invalid task identity')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',value['repo']):raise ValueError('Invalid repository')
    if not re.fullmatch('[a-f0-9]{40}',value['base_commit']):raise ValueError('Invalid base commit')
    if any(not isinstance(value[k],str) for k in ('version','problem_statement','hints_text','patch','test_patch')):raise ValueError('Task text must be strings')
    if not value['problem_statement'] or not value['test_patch']:raise ValueError('Missing instruction or tests')
    for key in ('FAIL_TO_PASS','PASS_TO_PASS'):
        tests=value[key]
        if not isinstance(tests,list) or any(not isinstance(t,str) or not t for t in tests) or len(set(tests))!=len(tests):raise ValueError('Invalid test list')
    if not value['FAIL_TO_PASS'] or set(value['FAIL_TO_PASS'])&set(value['PASS_TO_PASS']):raise ValueError('Invalid test partition')
    return value


def public_task(value):
    validate_task(value)
    return {key:value[key] for key in ('instance_id','repo','base_commit','problem_statement')}


def prepare(source,instance_ids,out):
    source,out=Path(source),Path(out)
    if hashlib.sha256(source.read_bytes()).hexdigest()!=SOURCE_SHA256:raise ValueError('Pinned SWE-Gym-Lite source changed')
    import pyarrow.parquet as pq
    rows={row['instance_id']:validate_task(row) for row in pq.read_table(source).to_pylist()}
    if not instance_ids or len(set(instance_ids))!=len(instance_ids) or set(instance_ids)-set(rows):raise ValueError('Select distinct IDs present in the pinned dataset')
    out.mkdir(mode=0o700,parents=True,exist_ok=False)
    tasks=[]
    for identity in instance_ids:
        path=out/(identity+'.json');path.write_text(json.dumps(rows[identity],indent=2)+'\n');path.chmod(0o600)
        tasks.append(public_task(rows[identity]))
    (out/'prompts.jsonl').write_text(''.join(json.dumps(value)+'\n' for value in tasks))
    (out/'manifest.json').write_text(json.dumps(dict(schema='dsec.swegym.dataset.v1',source=SOURCE_ID,split='train',source_sha256=SOURCE_SHA256,instance_ids=instance_ids),indent=2)+'\n')
    return dict(instances=len(tasks),output=str(out))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--instance-id',action='append',required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();print(json.dumps(prepare(args.source,args.instance_id,args.out)))


if __name__=='__main__':main()
