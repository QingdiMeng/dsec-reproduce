from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import signal
import subprocess
import sys
import unittest
import uuid

from dsec_openhands.guest import Shell, Tools


class ShellTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.shell = Shell(str(self.root))
        self.addCleanup(self.shell.close)

    def test_environment_and_directory_persist_between_calls(self):
        (self.root / 'sub').mkdir()
        self.assertEqual(self.shell.run('export DSEC_VALUE=kept; cd sub', True, 2)['exit_code'], 0)
        value = self.shell.run('printf "$DSEC_VALUE"; pwd', True, 2)
        self.assertEqual(value['exit_code'], 0)
        self.assertIn('kept' + str(self.root / 'sub'), value['content'])
        self.assertEqual(value['cwd'], str(self.root / 'sub'))

    def test_multiline_command_has_one_completion_and_its_final_exit_code(self):
        value = self.shell.run('printf first\nprintf second\nfalse', True, 2)
        self.assertEqual(value['exit_code'], 1)
        self.assertEqual(value['content'], 'firstsecond')
        following = self.shell.run('printf following', True, 2)
        self.assertEqual(following['exit_code'], 0)
        self.assertEqual(following['content'], 'following')

    def test_heredoc_followed_by_command_does_not_shift_later_observations(self):
        value = self.shell.run("cat > sample.py <<'PY'\nprint('sample')\nPY\ncat sample.py", True, 2)
        self.assertEqual(value['exit_code'], 0)
        self.assertEqual(value['content'].replace('\r\n', '\n'), "print('sample')\n")
        value = self.shell.run('printf own-output; false', True, 2)
        self.assertEqual(value['exit_code'], 1)
        self.assertEqual(value['content'], 'own-output')

    def test_foreground_stdin_and_poll_have_real_process_semantics(self):
        value = self.shell.run('read -r value; printf "accepted:%s" "$value"', False, .1)
        self.assertEqual(value['exit_code'], -1)
        value = self.shell.run('hello world', True, 2)
        self.assertEqual(value['exit_code'], 0)
        self.assertIn('accepted:hello world', value['content'])

    def test_activation_prompt_decoration_is_not_returned_as_command_output(self):
        shell = Shell(str(self.root), 'PS1="(task) $PS1"; export DSEC_ACTIVATED=yes')
        self.addCleanup(shell.close)
        value = shell.run('printf "$DSEC_ACTIVATED"', True, 2)
        self.assertEqual(value['content'], 'yes')
        self.assertEqual(value['exit_code'], 0)

    def test_background_launcher_signal_disposition_does_not_disable_interrupt(self):
        from dsec_openhands import guest
        program = '''import json,runpy,sys
Shell=runpy.run_path(sys.argv[1])['Shell']
shell=Shell(sys.argv[2])
try:
    shell.run('sleep 100', False, .1)
    print(json.dumps(shell.run('ctrl+c', True, 2)))
finally: shell.close()
'''
        value = subprocess.run([sys.executable, '-c', program, guest.__file__, str(self.root)],
            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_IGN),
            capture_output=True, text=True, timeout=10)
        self.assertEqual(value.returncode, 0, value.stderr)
        self.assertEqual(json.loads(value.stdout)['exit_code'], 130)

    def test_interrupt_and_timeout_preserve_the_shell(self):
        self.shell.run('export DSEC_VALUE=kept', True, 2)
        self.assertEqual(self.shell.run('sleep 12345', False, .1)['exit_code'], -1)
        self.assertEqual(self.shell.run('ctrl+c', True, 2)['exit_code'], 130)
        value = self.shell.run('sleep 12345', True, .1)
        self.assertTrue(value['timed_out'])
        self.assertEqual(value['exit_code'], 130)
        self.assertEqual(self.shell.run('printf "$DSEC_VALUE"', True, 2)['content'], 'kept')

    def test_large_output_is_bounded_and_exit_code_retained(self):
        value = self.shell.run("printf '%100000s' end; false", True, 3)
        self.assertTrue(value['truncated'])
        self.assertLess(len(value['content']), 13000)
        self.assertEqual(value['exit_code'], 1)


class RequestIdentityTests(unittest.TestCase):
    def test_competing_duplicates_execute_once_and_changed_payload_or_epoch_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            tools = Tools(dict(cwd=root, timeout_seconds=3))
            try:
                request = dict(id=uuid.uuid4().hex, epoch=tools.epoch, kind='bash',
                               command='printf x >> effect', blocking=True)
                barrier = threading.Barrier(2)
                def submit():
                    barrier.wait()
                    return tools.execute(request)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first, second = pool.submit(submit), pool.submit(submit)
                    self.assertEqual(first.result(), second.result())
                self.assertEqual((Path(root) / 'effect').read_text(), 'x')
                with self.assertRaises(ValueError):
                    tools.execute(dict(request, command='printf y >> effect'))
                with self.assertRaises(ValueError):
                    tools.execute(dict(request, id=uuid.uuid4().hex, epoch=uuid.uuid4().hex))
                self.assertEqual((Path(root) / 'effect').read_text(), 'x')
            finally:
                tools.close()
