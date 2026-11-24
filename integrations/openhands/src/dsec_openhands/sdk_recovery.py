"""Bounded metadata-only Stop hook for unparsed MiniCPM function markup."""
import argparse
import json
from pathlib import Path
import re
import shlex
import sys


def function_names(message):
    if message.get('tool_calls'):
        return []
    text='\n'.join(c.get('text','') for c in message.get('content',[]) if c.get('type')=='text')
    # Quoted code examples in normal answers are not attempted native calls.
    text=re.sub(r'```[^\n]*\n.*?```|~~~[^\n]*\n.*?~~~|`[^`\n]+`','',text,flags=re.S)
    return re.findall(r'<function\s+name=[\'\"]([^\'\"]+)[\'\"]',text)


class UnparsedToolRecovery:
    def __init__(self, directory, *, tool_names, limit):
        if type(limit) is not int or not 1<=limit<=10:
            raise ValueError('Unparsed-tool recovery limit must be between 1 and 10')
        self.path=Path(directory)/'stop-decision.json'
        self.tool_names=tool_names
        self.limit=limit
        self.attempts=[]
        self.last_unparsed_response=None
        self.command=shlex.join([sys.executable,'-I','-m','dsec_openhands.sdk_recovery',str(self.path)])
        self.decision={'decision':'allow'}
        self._save()

    def _save(self):
        self.path.write_text(json.dumps(self.decision))
        self.path.chmod(0o600)
        path=self.path.with_name('tool-recovery.json')
        path.write_text(json.dumps(self.summary(),indent=2));path.chmod(0o600)

    def summary(self):
        return dict(profile='minicpm5-stop-v1',limit=self.limit,attempts=self.attempts,
                    last_unparsed_response=self.last_unparsed_response)

    def on_event(self,event):
        value=event.model_dump(mode='json')
        if value.get('kind')=='HookExecutionEvent' and value.get('hook_command')==self.command:
            if (not value.get('success') or value.get('exit_code')!=0 or
                    bool(value.get('blocked'))!=(self.decision['decision']=='deny')):
                raise RuntimeError('Unparsed-tool Stop hook failed; episode cannot be scored')
            return
        if value.get('source')!='agent' or value.get('kind') not in ('ActionEvent','MessageEvent'):
            return
        self.decision={'decision':'allow'}
        self.last_unparsed_response=None
        if value['kind']=='MessageEvent':
            names=function_names(value.get('llm_message',{}))
            if names:
                self.last_unparsed_response=value.get('llm_response_id')
                denied=len(self.attempts)<self.limit
                available=sorted(self.tool_names())
                self.attempts.append(dict(response_id=value.get('llm_response_id'),
                    names=names,available_tools=available,feedback_requested=denied))
                if denied:
                    self.decision=dict(decision='deny',reason=(
                        'Your previous response contained unparsed function-call markup '
                        f'for {names!r}. It produced no structured tool call; no operation '
                        'from that response was executed. Available tools: '+', '.join(available)+
                        '. If you intend to use a tool, call an advertised tool through '
                        'the native tool interface with its required arguments. '
                        'Otherwise provide your final answer without function-call markup.'))
        self._save()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('decision',type=Path)
    args=parser.parse_args()
    event=json.load(sys.stdin)
    if event.get('event_type')!='Stop':
        raise ValueError('Only Stop hook events are accepted')
    print(args.decision.read_text())


if __name__=='__main__':
    main()
