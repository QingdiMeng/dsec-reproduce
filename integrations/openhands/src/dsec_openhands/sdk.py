"""OpenHands SDK tool binding; agent loop and tool semantics stay upstream.

Use in a dedicated controller process with a private, empty HOME and metadata
workspace. Those paths contain conversation evidence, never task repositories.
All task execution goes through the API-owned sandbox, including interrupts.
"""
from importlib.metadata import version
from pathlib import Path
import threading

from pydantic import PrivateAttr
from openhands.sdk import Agent, Conversation, Tool
from openhands.sdk.context import AgentContext
from openhands.sdk.tool import ToolAnnotations, ToolExecutor, register_tool
from openhands.sdk.workspace import LocalWorkspace
from openhands.tools.terminal.definition import TerminalAction, TerminalObservation, TerminalTool
from openhands.tools.file_editor.definition import FileEditorAction, FileEditorObservation, FileEditorTool

from . import SDK_VERSION, sdk_guest
from .transport import APISandbox


class SDKSandbox(APISandbox):
    response_limit = 1040384  # Native HTTP ceiling; incomplete results remain UNKNOWN.
    separate_interrupt_session = True
    companion = sdk_guest
    preflight = ('from importlib.metadata import version; '
                 'assert version("openhands-sdk") == version("openhands-tools") == "1.58.2"; '
                 'import openhands.tools.terminal, openhands.tools.file_editor')
    initialize_python = False


class ControllerWorkspace(LocalWorkspace):
    """Local conversation metadata only. No inherited host execution methods."""
    _binding: object = PrivateAttr()

    def execute_command(self, *args, **kwargs):
        raise RuntimeError('Controller command execution is disabled; use the DSec terminal tool')

    def file_upload(self, *args, **kwargs):
        raise RuntimeError('Controller workspace file transfer is disabled; use the DSec API')

    file_download = file_upload

    def git_changes(self, *args, **kwargs):
        raise RuntimeError('Controller workspace is metadata, not the task repository')

    git_diff = git_changes


class Binding:
    def __init__(self, sandbox):
        self.sandbox = sandbox
        self.lock = threading.Lock()
        self.failure = None
        self.failure_reported = False
        self.tool_recovery = None

    def check(self):
        if self.failure is not None:
            raise RuntimeError('DSec tool transport failed; episode cannot continue') from self.failure

    def execute(self, kind, action, observation_type, conversation):
        with self.lock:
            self.check()
            try:
                value = self.sandbox.action(kind, action=action.model_dump(mode='json'))
                return observation_type.model_validate(value)
            except BaseException as exc:
                # Never call conversation.pause() from a tool worker: the
                # upstream step can own its state lock while waiting for us.
                # Fence workers now; abort once at the event callback boundary.
                self.failure = exc
                raise

    def on_event(self, event):
        if self.failure is not None and not self.failure_reported:
            self.failure_reported = True
            self.check()


class RemoteExecutor(ToolExecutor):
    def __init__(self, binding, kind, observation_type):
        self.binding, self.kind, self.observation_type = binding, kind, observation_type

    def __call__(self, action, conversation=None):
        return self.binding.execute(self.kind, action, self.observation_type, conversation)

    def interrupt(self):
        if self.kind == 'terminal':
            try:
                self.binding.sandbox.action('interrupt')
            except BaseException as exc:
                self.binding.failure = exc
                raise


def definitions(state, kind, cls, action_type, observation_type):
    if not isinstance(state.workspace, ControllerWorkspace):
        raise TypeError('DSec tools require a DSec-bound controller workspace')
    binding = state.workspace._binding
    remote = binding.sandbox.guest_description['tools'][kind]
    return [cls(action_type=action_type, observation_type=observation_type,
                description=remote['description'], annotations=ToolAnnotations(**remote['annotations']),
                executor=RemoteExecutor(binding, kind, observation_type))]


class DSecTerminal(TerminalTool):
    @classmethod
    def create(cls, conv_state):
        return definitions(conv_state, 'terminal', TerminalTool, TerminalAction, TerminalObservation)


class DSecFileEditor(FileEditorTool):
    @classmethod
    def create(cls, conv_state):
        return definitions(conv_state, 'file_editor', FileEditorTool, FileEditorAction, FileEditorObservation)


register_tool('dsec_terminal', DSecTerminal)
register_tool('dsec_file_editor', DSecFileEditor)


def create_conversation(sandbox, llm, *, metadata_dir, home_dir, max_iterations=100, callbacks=(), condenser=None,
                        unparsed_tool_recovery=None, recovery_limit=3):
    """Construct the official agent with only guest-bound coding tools.

    The caller owns sandbox.close(), including failures before this returns.
    Conversation.close() cannot retire a shared sandbox implicitly.
    """
    for package in ('openhands-sdk', 'openhands-tools'):
        if version(package) != SDK_VERSION:
            raise RuntimeError('Controller SDK version differs: ' + package)
    home = Path(home_dir).resolve(strict=True)
    from openhands.sdk.llm.llm_profile_store import _DEFAULT_PROFILE_DIR
    from openhands.sdk.plugin.discovery import USER_PLUGINS_DIRS
    from openhands.sdk.utils.path import get_user_persistence_dir
    cached_paths = [_DEFAULT_PROFILE_DIR, *USER_PLUGINS_DIRS, get_user_persistence_dir()]
    if (Path.home().resolve() != home or any(home.iterdir()) or
            any(not Path(path).resolve().is_relative_to(home) for path in cached_paths) or
            any((parent / '.git').exists() for parent in (home, *home.parents))):
        raise ValueError('Start a dedicated controller process with a new empty HOME before importing OpenHands')
    root = Path(metadata_dir)
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    # Upstream discovers plugins from the workspace's enclosing Git root too.
    # Keep its metadata workspace under the sterile HOME, even when evidence
    # is saved inside a researcher's source checkout.
    workspace = ControllerWorkspace(working_dir=home / 'workspace')
    binding = Binding(sandbox)
    workspace._binding = binding
    context = AgentContext(load_user_skills=False, load_project_skills=False,
        load_public_skills=False, load_memory=False,
        system_message_suffix='All terminal and file_editor operations execute in the DSec sandbox. '
            'The task repository is at ' + sandbox.cwd + '. The controller stores conversation metadata only.')
    agent = Agent(llm=llm, tools=[Tool(name='dsec_terminal'), Tool(name='dsec_file_editor')],
        agent_context=context, system_prompt_kwargs={'soul_content': ''}, condenser=condenser)
    hook_config=None
    extra_callbacks=[]
    if unparsed_tool_recovery not in (None,'minicpm5'):
        raise ValueError('Unsupported unparsed-tool recovery profile')
    if unparsed_tool_recovery:
        from openhands.sdk.hooks.config import HookConfig, HookMatcher, HookDefinition
        from .sdk_recovery import UnparsedToolRecovery
        binding.tool_recovery=UnparsedToolRecovery(root,tool_names=lambda:agent.tools_map.keys(),limit=recovery_limit)
        hook_config=HookConfig(stop=[HookMatcher(hooks=[HookDefinition(
            command=binding.tool_recovery.command,timeout=5)])])
        extra_callbacks=[binding.tool_recovery.on_event]
    conversation = Conversation(agent=agent, workspace=workspace,
        persistence_dir=root / 'events', max_iteration_per_run=max_iterations,
        callbacks=[*callbacks, binding.on_event, *extra_callbacks], visualizer=None,hook_config=hook_config)
    return conversation, binding
