"""Lossless Thinking compatibility at the pinned OpenHands LLM boundary.

The old provider converter and agent history omit reasoning_content. Restore it
by exact tool-call identity without changing actions, observations or prompts.
Qwen retains it in the current user/tool chain; DeepSeek tools require all
assistant turns, including replies without tools. Provider bodies are evidence,
not a replacement for native sampled tokens/logprobs in an RL connector.
"""
import copy
import json
from pathlib import Path
import threading


def tool_identity(message):
    return tuple((call['id'], call.get('type', 'function'),
                  call['function']['name'], call['function']['arguments'])
                 for call in message.get('tool_calls') or [])


def text_content(message):
    value = message.get('content') or ''
    if isinstance(value, list):
        return ''.join(item.get('text', '') for item in value).strip()
    return value.strip()


def reasoning_scope(options):
    extra = options.get('sampling_extra', {})
    qwen = extra.get('chat_template_kwargs', {}).get('enable_thinking', False)
    deepseek = extra.get('thinking', {}).get('type') == 'enabled'
    if qwen and deepseek:
        raise ValueError('Conflicting provider Thinking contracts')
    return ('deepseek_all_assistant_turns' if deepseek else
            'qwen_current_user_tool_chain' if qwen else None)


def assistant_identity(message):
    content = message.get('content') or ''
    if isinstance(content, list):
        content = '\n'.join(item.get('text', '') for item in content)
    return content, tool_identity(message)


class ThinkingHistory:
    def __init__(self, scope='qwen_current_user_tool_chain'):
        if scope not in ('qwen_current_user_tool_chain', 'deepseek_all_assistant_turns'):
            raise ValueError('Unknown provider reasoning history scope')
        self.scope = scope
        self.responses = {}
        self.assistant_turns = []

    def remember(self, message):
        identity = tool_identity(message)
        reasoning = message.get('reasoning_content')
        if reasoning is not None and not isinstance(reasoning, str):
            raise ValueError('Provider reasoning_content must be a string or null')
        if identity and identity in self.responses:
            raise ValueError('Provider reused a tool-call identity')
        if identity:
            self.responses[identity] = reasoning
        self.assistant_turns.append((assistant_identity(message), reasoning))

    def restore(self, messages):
        messages = copy.deepcopy(messages)
        if self.scope == 'deepseek_all_assistant_turns':
            assistants = [m for m in messages if m['role'] == 'assistant']
            if len(assistants) != len(self.assistant_turns):
                raise ValueError('Assistant history lost a provider response')
            for message, (identity, reasoning) in zip(assistants, self.assistant_turns):
                if assistant_identity(message) != identity:
                    raise ValueError('Assistant history order/content differs from provider response')
                if isinstance(message.get('content'), list) and any(
                        item.get('type') != 'text' for item in message['content']):
                    raise ValueError('DeepSeek coding assistant history must be text-only')
                # The DeepSeek assistant schema takes a string, while the old
                # OpenHands tool-history serializer emits text-part arrays.
                message['content'] = identity[0]
                message.pop('reasoning_content', None)
                if reasoning is not None:
                    message['reasoning_content'] = reasoning
            return messages
        last_user = None
        for index, message in enumerate(messages):
            content = text_content(message)
            if message['role'] == 'user' and not (
                    content.startswith('<tool_response>') and content.endswith('</tool_response>')):
                last_user = index
        if last_user is None:
            raise ValueError('Thinking history requires a user query')
        for index, message in enumerate(messages):
            if message['role'] != 'assistant':
                continue
            # Match the Qwen template's current-user boundary. It also handles
            # inline <think> content itself; we only restore the separate field.
            message.pop('reasoning_content', None)
            identity = tool_identity(message)
            if index > last_user and identity:
                if identity not in self.responses:
                    raise ValueError('Unmatched assistant tool history; refusing guessed reasoning')
                reasoning = self.responses[identity]
                if reasoning is not None:
                    message['reasoning_content'] = reasoning
        return messages


class ThinkingBridge:
    def __init__(self, config, directory, scope='qwen_current_user_tool_chain'):
        self.config = config
        self.directory = Path(directory) / 'provider'
        self.directory.mkdir(mode=0o700)
        self.history = ThinkingHistory(scope)
        self.lock = threading.Lock()

    def complete(self, original, args, kwargs, record, save):
        import httpx
        from openai import OpenAI

        if not self.lock.acquire(blocking=False):
            raise RuntimeError('Concurrent use of a single OpenHands Thinking history')
        try:
            kwargs = dict(kwargs, messages=self.history.restore(kwargs['messages']))
            record['request']['messages'] = copy.deepcopy(kwargs['messages'])
            bodies = []

            def request_hook(request):
                body = json.loads(request.content)
                expected = {tool_identity(m): m['reasoning_content']
                            for m in kwargs['messages'] if 'reasoning_content' in m}
                actual = {tool_identity(m): m['reasoning_content']
                          for m in body['messages'] if 'reasoning_content' in m}
                if actual != expected:
                    raise ValueError('Provider request lost restored Thinking history')
                record['provider_request'] = body
                save()

            def response_hook(response):
                raw = response.read()
                destination = self.directory / f"{record['index']:04d}.response.json"
                with destination.open('xb') as stream:
                    destination.chmod(0o600)
                    stream.write(raw)
                record['provider_response_file'] = str(destination.relative_to(self.directory.parent))
                record['provider_status'] = response.status_code
                bodies.append(json.loads(raw))
                save()

            with httpx.Client(timeout=self.config.timeout,
                    event_hooks={'request': [request_hook], 'response': [response_hook]}) as http:
                with OpenAI(api_key=self.config.api_key, base_url=self.config.base_url,
                            timeout=self.config.timeout, max_retries=0, http_client=http) as client:
                    result = original(*args, **dict(kwargs, client=client))
            if len(bodies) != 1 or len(bodies[0].get('choices', [])) != 1:
                raise ValueError('Expected exactly one provider response and choice')
            raw = bodies[0]
            message = raw['choices'][0]['message']
            normalized = result.choices[0].message.model_dump()
            if (raw['id'] != result.id or tool_identity(message) != tool_identity(normalized)
                    or (message.get('content') or '') != (normalized.get('content') or '')):
                raise ValueError('Provider/normalized response identity or action differs')
            self.history.remember(message)
            if message.get('reasoning_content') is not None:
                result.choices[0].message['reasoning_content'] = message['reasoning_content']
            return result
        finally:
            self.lock.release()
