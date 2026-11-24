"""Collect repository changes without staging untracked nested repositories."""
import shlex


CAPTURE_SOURCE = '''import json, os, stat, subprocess, sys
from pathlib import Path

def git(*args, **options):
    return subprocess.run(['git', '--literal-pathspecs', *args], check=True, **options)

paths = git('ls-files', '--others', '--exclude-standard', '-z', stdout=subprocess.PIPE).stdout
files, skipped = [], []
for raw in paths.split(b'\\0'):
    if not raw:
        continue
    path = os.fsdecode(raw)
    mode = os.lstat(path).st_mode
    if stat.S_ISREG(mode) or stat.S_ISLNK(mode):
        files.append(path)
    else:
        skipped.append(dict(path=path, reason='untracked non-file; not part of the repository patch'))
for start in range(0, len(files), 100):
    git('add', '-N', '--', *files[start:start + 100])
with Path(sys.argv[1]).open('wb') as stream:
    git('diff', '--binary', 'HEAD', stdout=stream)
print(json.dumps(dict(added_intent_files=len(files), skipped=skipped)))
'''


def capture_command():
    # Run inside the prepared task environment. Literal pathspecs preserve file
    # names containing Git magic, whitespace or newlines; symlinks stay links.
    return 'python -c ' + shlex.quote(CAPTURE_SOURCE) + ' /work/solution.patch'
