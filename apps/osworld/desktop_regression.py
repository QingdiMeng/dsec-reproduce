"""Replay desktop actions through the public DSec FullVM SDK."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re
import shlex
import time
import uuid

KEY_ALIAS = {'enter': 'ret', 'escape': 'esc', 'space': 'spc'}
TEXT_KEYS = {' ': ('spc', False), ':': ('semicolon', True), ';': ('semicolon', False),
             '.': ('dot', False), '-': ('minus', False), '_': ('minus', True),
             '/': ('slash', False), '$': ('4', True), '&': ('7', True),
             '=': ('equal', False), "'": ('apostrophe', False), ',': ('comma', False)}


def key_events(keys):
    keys = [KEY_ALIAS.get(key, key) for key in keys]
    if not keys or len(keys) > 4 or any(not re.fullmatch(r'[a-z0-9_]+', key) for key in keys):
        raise ValueError('Invalid key chord')
    return [dict(type='key', data=dict(down=down, key=dict(type='qcode', data=key)))
            for down, sequence in ((True, keys), (False, reversed(keys))) for key in sequence]


def pointer(xy, size):
    return [dict(type='abs', data=dict(axis=axis, value=round(32767*p/(extent-1))))
            for axis, p, extent in zip('xy', xy, size, strict=True) if 0 <= p < extent]


def button(down):
    return [dict(type='btn', data=dict(button='left', down=down))]


def batches(action, size):
    op = action['op']
    if op == 'key':
        return [key_events(action['keys'])]
    if op == 'text':
        text = action['text']
        if not 1 <= len(text) <= 256:
            raise ValueError('Invalid text length')
        result = []
        for char in text:
            if char in TEXT_KEYS:
                key, shift = TEXT_KEYS[char]
            elif re.fullmatch(r'[a-zA-Z0-9]', char):
                key, shift = char.lower(), char.isupper()
            else:
                raise ValueError(f'Unsupported typed character {char!r}')
            result.append(key_events(['shift', key] if shift else [key]))
        return result
    if op in ('click', 'double_click', 'drag'):
        move = pointer(action['at'], size)
        if len(move) != 2:
            raise ValueError('Pointer target is outside the screenshot')
        if op == 'drag':
            destination = pointer(action['to'], size)
            if len(destination) != 2:
                raise ValueError('Drag destination is outside the screenshot')
            return [move, button(True), destination, button(False)]
        return [move, *([button(True), button(False)] * (2 if op == 'double_click' else 1))]
    if op == 'observe':
        return []
    raise ValueError(f'Unknown action {op!r}')


async def setup(box, workbook):
    guest = '/work/'+workbook.name
    await box.write_file(guest, workbook.read_bytes())
    for _ in range(100):
        ready = await box.run_shell(
            "DISPLAY=:0 xdotool search --onlyvisible --name '^DSecDesktop$'")
        if ready['exit_code'] == 0:
            break
        await asyncio.sleep(.2)
    else:
        raise RuntimeError('Xfce desktop did not become ready')
    command = ('export DISPLAY=:0 HOME=/work XDG_RUNTIME_DIR=/run/user/1000; '
               'nohup libreoffice -env:UserInstallation=file:///work/lo-regression '
               '--norestore --nofirststartwizard --calc '+shlex.quote(guest)
               +' </dev/null >/work/lo-regression.log 2>&1 &')
    result = await box.run_shell(command)
    if result['exit_code'] != 0:
        raise RuntimeError(result['output'])
    title = '^'+re.escape(workbook.name)+' - LibreOffice Calc$'
    search = 'DISPLAY=:0 xdotool search --onlyvisible --name '+shlex.quote(title)
    for _ in range(120):
        result = await box.run_shell(search)
        if result['exit_code'] == 0 and result['output'].strip().isdigit():
            window_id = result['output'].strip()
            active = await box.run_shell('DISPLAY=:0 xdotool getactivewindow getwindowname')
            if workbook.name not in active['output'] and 'Tip of the Day' not in active['output']:
                await box.run_shell('DISPLAY=:0 xdotool windowactivate '+window_id)
            await asyncio.sleep(.35)
            return
        await asyncio.sleep(.2)
    diagnostics = await box.run_shell(
        'tail -20 /work/lo-regression.log; DISPLAY=:0 xdotool getactivewindow getwindowname')
    raise RuntimeError('Calc window did not become ready: '+diagnostics['output'])


def frame_metadata(shot, seq, phase, returned_ns):
    return dict(id=f'{seq:03d}:{phase}', step=seq, phase=phase, returned_monotonic_ns=returned_ns,
                sha256=hashlib.sha256(shot['data']).hexdigest())


async def capture(box, out, seq, action, *, events=None, observe=None, expected_size=(1024, 768)):
    row = dict(step=seq, action=action, requests=[])
    screen = await box.screenshot()
    before_ns = time.monotonic_ns()
    if expected_size is not None and (screen['width'], screen['height']) != expected_size:
        raise ValueError(f'This desktop replay requires a {expected_size[0]}×{expected_size[1]} display')
    t0 = time.monotonic_ns()
    scheduled = batches(action, (screen['width'], screen['height']))
    if events is not None:
        scheduled = events(action, scheduled)
    for batch in scheduled:
        request_id = uuid.uuid4().hex
        t = time.monotonic_ns()
        receipt = await box.desktop_input(batch, request_id=request_id)
        row['requests'].append(dict(request_id=request_id, events=batch, receipt=receipt,
                                    elapsed_ms=round((time.monotonic_ns()-t)/1e6, 2)))
        if action['op'] in ('click', 'double_click', 'drag'):
            await asyncio.sleep(.075)
    await asyncio.sleep(min(2, max(0, action.get('settle', .25))))
    after = await box.screenshot()
    after_ns = time.monotonic_ns()
    delivered = after if observe is None else observe(action, screen, after)
    if delivered is not screen and delivered is not after:
        raise ValueError('Observation hook must choose one of the captured frames')
    before_frame = frame_metadata(screen, seq, 'before', before_ns)
    after_frame = frame_metadata(after, seq, 'after', after_ns)
    chosen = before_frame if delivered is screen else after_frame
    path = f'{seq:03d}.png'
    if delivered is screen:
        after_frame['path'] = f'{seq:03d}-after.png'
        (out/after_frame['path']).write_bytes(after['data'])
    chosen['path'] = path
    row['frames'] = dict(before=before_frame, after=after_frame, delivered=chosen['id'])
    window = await box.run_shell('DISPLAY=:0 xdotool getactivewindow getwindowname')
    row['observation'] = dict(path=path, width=delivered['width'], height=delivered['height'],
                              sha256=chosen['sha256'],
                              window=window['output'].strip() if window['exit_code'] == 0 else None)
    row['elapsed_ms'] = round((time.monotonic_ns()-t0)/1e6, 2)
    (out/path).write_bytes(delivered['data'])
    with (out/'trace.jsonl').open('a', encoding='utf-8') as trace:
        trace.write(json.dumps(row, ensure_ascii=False)+'\n')
    return row


async def replay(config, workbook, out, *, actions, events=None, observe=None):
    try:
        from dsec.sdk import DSecClient, DSecFullVMRunArgs
    except ImportError as exc:
        raise RuntimeError('Install a DSec SDK with FullVM desktop support') from exc
    from dsec._credentials import secret_file

    if not isinstance(actions, list) or not 1 <= len(actions) <= 128:
        raise ValueError('Expected 1..128 actions')
    out.mkdir(parents=True)
    spec = config['paths']['fullvm']
    async with DSecClient(**config['client'], token=secret_file(config['token_file'])) as client:
        for _ in range(100):
            views = (await client.node_status())['nodes'].values()
            if any(view['available']['concurrency'] for view in views):
                break
            await asyncio.sleep(.1)
        else:
            raise TimeoutError('No FullVM placement capacity')
        box = await client.run_fullvm(DSecFullVMRunArgs(**spec['spec']), resource_demand=spec['demand'])
        report = dict(status='failed', sandbox_id=box.id, workbook=workbook.name, steps=0)
        try:
            await setup(box, workbook)
            await capture(box, out, 0, dict(op='observe'))
            for seq, action in enumerate(actions, 1):
                await capture(box, out, seq, action, events=events, observe=observe)
                report['steps'] = seq
            guest_path = '/work/'+workbook.name
            (out/workbook.name).write_bytes(await box.read_file(guest_path, max_bytes=8*1024*1024))
            report['status'] = 'passed'
        finally:
            stopped = await box.stop()
            report['cleanup_complete'] = stopped.get('resource_cleanup_complete') is True
            (out/'replay-report.json').write_text(json.dumps(report, indent=2)+'\n')
            if not report['cleanup_complete']:
                raise RuntimeError('FullVM cleanup incomplete')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('config', 'workbook', 'output', 'actions'):
        parser.add_argument('--'+flag, type=Path, required=True)
    args = parser.parse_args()
    actions = json.loads(args.actions.read_text())
    print(json.dumps(asyncio.run(replay(json.loads(args.config.read_text()), args.workbook,
                                       args.output, actions=actions))))


if __name__ == '__main__':
    main()
