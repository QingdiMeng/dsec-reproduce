"""Same-principal API credential overlap and retirement in the IAM CAS."""
from dsec.control.iam import credential_key
from dsec.contracts.cluster import AuthorizationDenied, request_id
from dsec.contracts.requests import RequestConflict, request_digest


def rotate(iam,token,rotation_id,action,*,new_token=None):
    request_id(rotation_id)
    if action not in ('prepare','complete','cancel','get'):raise ValueError('Unsupported credential rotation')
    key='credential-rotations/'+rotation_id;caller=credential_key(token)
    target=credential_key(new_token) if action=='prepare' else None
    if action=='prepare' and caller==target:raise ValueError('A fresh replacement credential is required')
    def change(snapshot):
        principal,_,_=iam._authorize(snapshot,token,'root','iam.credentials')
        old=snapshot.read(key)
        if action=='prepare':
            digest=request_digest('credential.prepare',None,dict(source=caller,target=target))
            if old is not None:
                if old['digest']!=digest:raise RequestConflict('Rotation ID belongs to another credential pair')
                return old,{}
            if snapshot.read(target) is not None:raise RequestConflict('Replacement credential already exists or was revoked')
            record=dict(id=rotation_id,state='OVERLAP',principal=principal,source=caller,target=target,digest=digest)
            return record,{key:record,target:dict(principal=principal,revoked=False)}
        if old is None or caller not in (old['source'],old['target']):
            raise AuthorizationDenied('Rotation does not belong to this credential')
        if action=='get':return old,{}
        expected='target' if action=='complete' else 'source'
        state='COMPLETED' if action=='complete' else 'CANCELLED'
        if caller!=old[expected]:raise AuthorizationDenied('Completion needs the new credential; cancellation needs the source')
        if old['state']==state:return old,{}
        if old['state']!='OVERLAP':raise RequestConflict('Credential rotation is already final')
        retired=old['source' if action=='complete' else 'target']
        value=snapshot.read(retired)
        if value is None:raise RequestConflict('Rotation credential record is missing')
        record=dict(old,state=state)
        return record,{key:record,retired:dict(value,revoked=True)}
    record=iam._change(change,action='credential.rotation.'+action)
    return {key:record[key] for key in ('id','state','principal')}
