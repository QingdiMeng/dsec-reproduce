"""Opt-in coding conformance; no benchmark, model or trainer required."""
import asyncio
import json
import os
from pathlib import Path
import traceback
import threading
import unittest
import uuid


@unittest.skipUnless(os.environ.get('DSEC_OPENHANDS_ACCEPTANCE_CONFIG'),
                     'requires an explicitly configured real DSec API')
class LinuxRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_upstream_coding_semantics_and_retirement(self):
        from dsec_openhands.runtime import DSecRuntime
        from openhands.core.config import AppConfig, SandboxConfig
        from openhands.events import EventStream
        from openhands.events.action import CmdRunAction, IPythonRunCellAction, FileReadAction, FileWriteAction
        from openhands.storage.local import LocalFileStore

        config = json.loads(Path(os.environ['DSEC_OPENHANDS_ACCEPTANCE_CONFIG']).read_text())
        out = Path(os.environ['DSEC_OPENHANDS_ACCEPTANCE_OUT'])
        out.mkdir(mode=0o700, parents=True, exist_ok=False)
        sid = uuid.uuid4().hex
        app = AppConfig(workspace_base=None, workspace_mount_path=None,
                        sandbox=SandboxConfig(timeout=config['command_timeout_ms'] // 1000))
        runtime = DSecRuntime(config=app, event_stream=EventStream(sid, LocalFileStore(str(out / 'events'))),
                              sid=sid, headless_mode=True, dsec_config=config)
        report = dict(status='running', checks=[])

        def check(name, condition, **facts):
            report['checks'].append(dict(name=name, passed=bool(condition), **facts))
            self.assertTrue(condition, name)

        async def shell(command, timeout=10, blocking=True):
            action = CmdRunAction(command=command)
            action.timeout = timeout
            action.blocking = blocking
            return await asyncio.to_thread(runtime.run, action)

        async def cell(code):
            action = IPythonRunCellAction(code=code)
            action.timeout = 30
            return (await asyncio.to_thread(runtime.run_ipython, action)).content

        try:
            await runtime.connect()
            directory = '/tmp/dsec-openhands-check-' + sid
            check('workspace_setup', (await shell('mkdir -p ' + directory + '; cd ' + directory +
                  '; export DSEC_CHECK=kept')).exit_code == 0)
            check('persistent_cwd_and_environment', 'kept' + directory in
                  (await shell('printf "$DSEC_CHECK"; pwd')).content)
            check('upstream_git_no_pager_init', 'git --no-pager' in (await shell('alias git')).content)
            value = await shell('printf first\nprintf second\nfalse')
            check('multiline_has_one_completion_and_final_exit', value.exit_code == 1 and value.content == 'firstsecond')
            value = await shell("cat > multiline.py <<'PY'\nprint('sample')\nPY\ncat multiline.py")
            check('heredoc_and_following_command_share_completion',
                  value.exit_code == 0 and value.content.replace('\r\n', '\n') == "print('sample')\n")
            value = await shell('printf own-output; false')
            check('next_observation_is_not_stale', value.exit_code == 1 and value.content == 'own-output')
            value = await shell('git init -q; seq 1 100 > pager-check.txt; git add pager-check.txt; '
                                'git commit -qm check; git show; git log -1', timeout=5)
            check('git_inspection_has_no_pager_wait', value.exit_code == 0 and '+100' in value.content)
            check('foreground_wait', (await shell('read -r answer; printf "answer:%s" "$answer"',
                  timeout=.1, blocking=False)).exit_code == -1)
            check('foreground_stdin', 'answer:hello' in (await shell('hello')).content)
            check('foreground_poll', (await shell('sleep .3; printf done', timeout=.1, blocking=False)).exit_code == -1)
            check('foreground_completion', 'done' in (await shell('')).content)
            await shell('sleep 100', timeout=.1, blocking=False)
            check('interrupt', (await shell('ctrl+c')).exit_code == 130)
            check('hard_timeout', (await shell('sleep 100', timeout=.1)).exit_code == 130)
            check('shell_state_survives_timeout', 'kept' in (await shell('printf "$DSEC_CHECK"')).content)
            await cell('saved_value = 42')
            check('persistent_python', '42' in await cell('print(saved_value)'))
            path = directory + '/editor.txt'
            await cell(f"print(file_editor(command='create', path={path!r}, file_text='old\\n'))")
            await cell(f"print(file_editor(command='str_replace', path={path!r}, old_str='old', new_str='new'))")
            check('upstream_editor', 'new' in (await shell('cat editor.txt')).content)
            await cell(f"print(file_editor(command='undo_edit', path={path!r}))")
            check('upstream_editor_undo', 'old' in (await shell('cat editor.txt')).content)
            await asyncio.to_thread(runtime.write, FileWriteAction(path='native.txt', content='one\ntwo\nthree'))
            value = await asyncio.to_thread(runtime.read, FileReadAction(path='native.txt', start=1, end=2))
            check('native_file_line_ranges', value.content == 'two\n', actual=value.content)
            # Cancelling the upstream awaiter must not strand its admitted SDK
            # request or race retirement against it. Keep the actual outcome.
            outcome = {}
            settled = threading.Event()
            action = CmdRunAction(command='sleep .6; printf admitted-complete')
            action.timeout = 5
            def execute():
                try:
                    value = runtime.run(action)
                    outcome.update(exit_code=value.exit_code, content=value.content)
                finally:
                    settled.set()
            awaiting = asyncio.create_task(asyncio.to_thread(execute))
            for _ in range(200):
                if runtime.backend.portal.has_pending():
                    break
                await asyncio.sleep(.01)
            else:
                self.fail('Native request was not submitted')
            awaiting.cancel()
            await asyncio.gather(awaiting, return_exceptions=True)
            await asyncio.to_thread(runtime.close)
            self.assertTrue(await asyncio.to_thread(settled.wait, 2))
            check('cancelled_awaiter_work_settles_before_retirement',
                  outcome.get('exit_code') == 0 and 'admitted-complete' in outcome.get('content', ''), **outcome)
            report['status'] = 'passed'
        except BaseException:
            report.update(status='failed', traceback=traceback.format_exc())
            raise
        finally:
            try:
                await asyncio.to_thread(runtime.close)
                self.assertEqual(runtime.backend.cleanup, 'stopped')
            except BaseException:
                report.update(status='failed', cleanup_traceback=traceback.format_exc())
                raise
            finally:
                report.update(cleanup=runtime.backend.cleanup, sandbox_id=runtime.backend.id,
                              operations=runtime.backend.operations)
                destination = out / 'report.json'
                destination.write_text(json.dumps(report, indent=2) + '\n')
                destination.chmod(0o600)
