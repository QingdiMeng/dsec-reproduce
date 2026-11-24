"""Explicit endpoint Thinking policy for the pinned SDK's message serializer.

SDK-native reasoning fields are retained; no response parsing or agent history
is replaced. Unknown endpoint aliases cannot rely on upstream model allowlists.
"""
from typing import Literal
from openhands.sdk import LLM


class EndpointLLM(LLM):
    thinking_history: Literal['disabled', 'current_user_turn', 'all_assistant_turns'] = 'disabled'

    def _to_chat_dicts(self, messages):
        values = super()._to_chat_dicts(messages)
        last_user = max((i for i, message in enumerate(messages) if message.role == 'user'), default=-1)
        for index, (message, value) in enumerate(zip(messages, values, strict=True)):
            if self.thinking_history == 'disabled':
                continue
            value.pop('reasoning_content', None)
            if message.role == 'assistant' and message.reasoning_content and (
                self.thinking_history == 'all_assistant_turns' or index > last_user
            ):
                value['reasoning_content'] = message.reasoning_content
        return values
