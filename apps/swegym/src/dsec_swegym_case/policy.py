"""Optional Qwen3.5/SGLang sampling; never imported by the DSec runtime.

Use actual sampled IDs and logprobs. Tool observations extend the token stream;
they never decode/re-encode, trim, or replace earlier policy tokens.
"""
import asyncio
import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping
from urllib.error import HTTPError
from urllib.request import Request,build_opener,ProxyHandler

TOOLS=[{'type':'function','function':{'name':name,'description':description,
    'parameters':{'type':'object','properties':{parameter:{'type':'string'}},
                  'required':[parameter],'additionalProperties':False}}}
    for name,parameter,description in (
        ('run_shell','command','Run a shell command in the persistent /testbed repository. Output includes exit status and timeouts.'),
        ('finish','summary','Finish after editing the repository and checking the fix. The repository diff is submitted.'))]

SYSTEM_PROMPT='''You are a coding agent fixing a repository issue. Inspect the repository,
edit its source files, and run focused checks. All run_shell commands start in
/testbed with the prepared testbed Conda environment activated; files persist
across calls, but shell processes do not. Shell commands
run for at most 30 seconds. Inspect command exit_code, timed_out and output;
recover from errors rather than assuming success. Use the provided function
calling format. You may call run_shell more than once in a reply: calls execute
in their written order. Call finish alone when done. Do not mix finish with shell
calls. Work from current source and a focused reproducer; avoid repeatedly
searching Git history without changing or testing the source. Do not change tests
to hide a failure. The evaluator supplies private tests
later. Explain neither a gold patch nor test answers: they are not available.'''

CODING_SAMPLING=dict(temperature=.6,top_p=.95,top_k=20,min_p=0.,
                     presence_penalty=0.,repetition_penalty=1.)


class ToolFormatError(ValueError):pass


def parse_calls(text,*,thinking=True):
    """Parse the Qwen3.5 template's XML dialect, validating the whole batch.

    A malformed later call prevents every call in that reply from executing.
    Thinking and narrative are retained in evidence, never interpreted as shell.
    """
    if text.count('</think>')>1 or (thinking and '</think>' not in text):
        raise ToolFormatError('Expected exactly one completed thinking segment')
    final=text.split('</think>')[-1]
    blocks=list(re.finditer(r'<tool_call>\s*(.*?)\s*</tool_call>',final,re.S))
    if not 1<=len(blocks)<=8:raise ToolFormatError('Expected 1–8 complete XML function calls')
    residue=re.sub(r'<tool_call>\s*.*?\s*</tool_call>','',final,flags=re.S)
    if '<tool_call' in residue or '</tool_call>' in residue or final[blocks[-1].end():].strip():
        raise ToolFormatError('Incomplete call or text after final function call')
    calls=[]
    for block in blocks:
        match=re.fullmatch(r'<function=(run_shell|finish)>\s*<parameter=(command|summary)>\n?(.*?)\n?</parameter>\s*</function>',block[1],re.S)
        if not match:raise ToolFormatError('Use the declared function and one required parameter')
        name,parameter,value=match.groups()
        if (parameter!=('command' if name=='run_shell' else 'summary') or not value.strip() or len(value)>16384
                or '</parameter>' in value or '<parameter=' in value):
            raise ToolFormatError('Invalid function parameter')
        calls.append(dict(name=name,arguments={parameter:value}))
    if any(v['name']=='finish' for v in calls) and len(calls)!=1:
        raise ToolFormatError('finish must be called alone')
    return calls


def checked_generation(value):
    ids=value.get('output_ids');meta=value.get('meta_info',{});rows=meta.get('output_token_logprobs')
    if (not isinstance(ids,list) or not ids or any(type(t) is not int or t<0 for t in ids)
            or not isinstance(rows,list) or len(rows)!=len(ids)):
        raise ValueError('Native output IDs and per-token logprobs are required and must align')
    for token,row in zip(ids,rows):
        if (not isinstance(row,(list,tuple)) or len(row)<2 or type(row[1]) is not int or row[1]!=token
                or type(row[0]) not in (int,float) or not math.isfinite(row[0])):
            raise ValueError('Sampled token/logprob identity mismatch')
    if not isinstance(value.get('text'),str) or not isinstance(meta.get('finish_reason'),dict):
        raise ValueError('Missing raw response or finish reason')
    return ids,[row[0] for row in rows]


class TokenContext:
    def __init__(self,tokenizer,instruction,*,thinking=True):
        self.tokenizer=tokenizer;self.thinking=thinking
        encoded=tokenizer.apply_chat_template([
            dict(role='system',content=SYSTEM_PROMPT),dict(role='user',content=instruction)],
            tools=TOOLS,enable_thinking=thinking,add_generation_prompt=True,tokenize=True,return_dict=False)
        self.ids=list(encoded['input_ids'] if isinstance(encoded,Mapping) else encoded)
        if not self.ids or any(type(token) is not int or token<0 for token in self.ids):
            raise ValueError('Chat tokenizer must return a flat sequence of integer token IDs')
        self.mask=[0]*len(self.ids);self.logprobs=[None]*len(self.ids);self.spans=[]
        self.end=tokenizer.encode('<|im_end|>',add_special_tokens=False)
        if len(self.end)!=1:raise ValueError('This adapter requires the Qwen3.5 message-end token')

    def generated(self,value,policy_version):
        ids,logprobs=checked_generation(value);start=len(self.ids)
        self.ids.extend(ids);self.mask.extend([1]*len(ids));self.logprobs.extend(logprobs)
        self.spans.append(dict(start=start,end=len(self.ids),policy_version=policy_version,
                               native_weight_versions=value['meta_info'].get('weight_versions')))

    def observe(self,observations):
        # Derive only the new tool-response suffix from the actual template.
        # Never round-trip the sampled assistant text through Jinja's trim or
        # reasoning extraction: doing so changes token identities and logprobs.
        messages=[dict(role='user',content='template-boundary'),dict(role='assistant',content='template-boundary')]
        options=dict(tools=TOOLS,enable_thinking=self.thinking,tokenize=False)
        before=self.tokenizer.apply_chat_template(messages,add_generation_prompt=False,**options)
        after=self.tokenizer.apply_chat_template(messages+[
            dict(role='tool',content=value) for value in observations],add_generation_prompt=True,**options)
        if not after.startswith(before):raise ValueError('Chat template does not provide an append-only tool suffix')
        closing='' if self.ids[-len(self.end):]==self.end else '<|im_end|>'
        ids=self.tokenizer.encode(closing+'\n'+after[len(before):],add_special_tokens=False)
        self.ids.extend(ids);self.mask.extend([0]*len(ids));self.logprobs.extend([None]*len(ids))

    def export(self):
        return dict(schema='dsec.swegym.tokens.v1',input_ids=self.ids,loss_mask=self.mask,
                    rollout_logprobs=self.logprobs,policy_spans=self.spans,
                    observation_masked=True,prior_sampled_tokens_preserved=True)


class SGLangPolicy:
    def __init__(self,endpoint,tokenizer,*,policy_version,thinking=True,
                 response_length=32768,context_length=65536,timeout=600):
        if (not policy_version or type(response_length) is not int or type(context_length) is not int
                or not 1<=response_length<context_length or not 1<=timeout<=1200):
            raise ValueError('Explicit policy version and bounded generation configuration required')
        self.endpoint=endpoint.rstrip('/')+'/generate';self.tokenizer=tokenizer
        self.version=policy_version;self.thinking=thinking;self.response_length=response_length
        self.context_length=context_length;self.timeout=timeout
        # Explicit model endpoint; do not route loopback requests through an
        # inherited HTTP proxy or silently retry an uncertain generation.
        self.opener=build_opener(ProxyHandler({}))

    def context(self,instruction):return TokenContext(self.tokenizer,instruction,thinking=self.thinking)

    def metadata(self):
        template=getattr(self.tokenizer,'chat_template','')
        return dict(schema='dsec.swegym.policy.v1',policy_version=self.version,
                    system_prompt=SYSTEM_PROMPT,tools=TOOLS,sampling_params=CODING_SAMPLING,
                    thinking=self.thinking,response_length=self.response_length,context_length=self.context_length,
                    tokenizer_class=type(self.tokenizer).__name__,
                    chat_template_sha256=hashlib.sha256(str(template).encode()).hexdigest())

    def request(self,context):
        remaining=self.context_length-len(context.ids)
        if remaining<=0:raise OverflowError('Context budget exhausted; history was not truncated')
        return dict(rid=uuid.uuid4().hex,input_ids=list(context.ids),
                    sampling_params=dict(CODING_SAMPLING,max_new_tokens=min(self.response_length,remaining)),
                    return_logprob=True,logprob_start_len=-1,return_text_in_logprobs=False,stream=False)

    async def generate(self,request,*,timeout=None):
        def send():
            req=Request(self.endpoint,data=json.dumps(request).encode(),headers={'Content-Type':'application/json'})
            try:
                with self.opener.open(req,timeout=min(self.timeout,timeout or self.timeout)) as response:
                    data=response.read(16*1024**2+1)
            except HTTPError as exc:
                detail=exc.read(8192).decode(errors='replace')
                raise RuntimeError(f'Model HTTP {exc.code}: {detail}') from exc
            if len(data)>16*1024**2:raise ValueError('Model response exceeds evidence budget')
            value=json.loads(data);checked_generation(value);return value
        return await asyncio.to_thread(send)
