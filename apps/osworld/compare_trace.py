"""Report the first divergent checkpoint in two DSec desktop traces."""
import argparse
import json
from pathlib import Path

from PIL import Image, ImageChops

# Windows and localized regions are compared. Full screen hashes would
# mistake desktop clock and cursor differences for execution divergence.
PIXEL_DELTA = 24
CHANGED_FRACTION = .025


def area_delta(a, b, roi):
    with Image.open(a) as left, Image.open(b) as right:
        if left.size != right.size:
            raise ValueError('Screenshot sizes differ')
        x0, y0, x1, y1 = roi
        if not (0 <= x0 < x1 <= left.width and 0 <= y0 < y1 <= left.height):
            raise ValueError('Checkpoint lies outside the screenshot')
        diff = ImageChops.difference(left.crop(roi).convert('RGB'), right.crop(roi).convert('RGB'))
        pixels = diff.tobytes()
        changed = sum(max(pixels[i:i+3]) >= PIXEL_DELTA for i in range(0, len(pixels), 3))
        return changed / (diff.width*diff.height)


def load_pair(reference, candidate):
    sources = [list(map(json.loads, (d/'trace.jsonl').read_text().splitlines()))
               for d in (reference, candidate)]
    if not sources[0] or len(sources[0]) != len(sources[1]) or any(
            (lhs['step'], lhs['action']) != (rhs['step'], rhs['action'])
            for lhs, rhs in zip(*sources)):
        raise ValueError('Replay inputs do not match')
    return list(zip(*sources))


def visual_difference(reference, candidate, pairs):
    checkpoints = [(lhs, rhs) for lhs, rhs in pairs if 'roi' in lhs['action']]
    if not checkpoints:
        raise ValueError('Replay has no screenshot checkpoints')
    for lhs, rhs in checkpoints:
        roi = lhs['action']['roi']
        a, b = lhs['observation'], rhs['observation']
        changed = area_delta(reference/a['path'],candidate/b['path'],roi)
        if a['window'] != b['window'] or changed > CHANGED_FRACTION:
            return dict(step=lhs['step'], checkpoint=lhs['action']['label'],
                        reference_window=a['window'],candidate_window=b['window'],
                        changed_fraction=round(changed,4),reference_frame=a['path'],
                        candidate_frame=b['path'])
    return None


def compare(reference, candidate):
    return visual_difference(reference, candidate, load_pair(reference, candidate))


def event_label(event):
    data = event['data']
    if event['type'] == 'abs':
        return f"{data['axis']}={data['value']}"
    label = data['key']['data'] if event['type'] == 'key' else data['button']
    return ('+' if data['down'] else '-') + label


def describe(row, held):
    initial = sorted(held) if held is not None else None
    submitted = []
    for request in row['requests']:
        events = request['events']
        count = request['receipt'].get('accepted')
        submitted.append(dict(events=[event_label(event) for event in events], accepted=count))
        if count != len(events):
            # An incomplete acknowledgement does not identify which events ran.
            held = None
        if held is not None:
            for event in events:
                if event['type'] == 'btn':
                    data = event['data']
                    if data['button'].startswith('wheel-'):
                        continue
                    if data['down']:
                        held.add(data['button'])
                    else:
                        held.discard(data['button'])
    frames = row.get('frames')
    source = None
    if frames:
        source = next(frame for frame in (frames['before'], frames['after'])
                      if frame['id'] == frames['delivered'])
        source = dict(delivered=source['id'], phase=source['phase'],
                      returned_monotonic_ns=source['returned_monotonic_ns'],
                      delivered_sha256=source['sha256'], after_sha256=frames['after']['sha256'],
                      after_path=frames['after'].get('path'))
    return dict(batches=submitted, buttons_before=initial, buttons_after=sorted(held) if held is not None else None,
                frame=source), held


def diagnose(reference, candidate):
    pairs = load_pair(reference, candidate)
    visible = visual_difference(reference, candidate, pairs)
    first_input = first_frame = None
    button_mismatches = []
    left_held, right_held = set(), set()
    for lhs, rhs in pairs:
        a, left_held = describe(lhs, left_held)
        b, right_held = describe(rhs, right_held)
        step = lhs['step']
        evidence = dict(step=step, action=lhs['action'], reference=a, candidate=b)
        if first_input is None and a['batches'] != b['batches']:
            first_input = evidence
        if a['buttons_after'] != b['buttons_after'] and len(button_mismatches) < 4:
            button_mismatches.append(dict(step=step, reference=a['buttons_after'], candidate=b['buttons_after']))
        if first_frame is None and a['frame'] and b['frame'] and a['frame']['phase'] != b['frame']['phase']:
            first_frame = evidence
    return dict(first_input_difference=first_input, first_frame_source_difference=first_frame,
                button_end_state_differences=button_mismatches, first_visible_difference=visible,
                frame_sources_available=all('frames' in row for pair in pairs for row in pair))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reference',type=Path)
    parser.add_argument('candidate',type=Path)
    parser.add_argument('--diagnose', action='store_true', help='Include input and frame-source evidence')
    args=parser.parse_args()
    inspect = diagnose if args.diagnose else compare
    print(json.dumps(inspect(args.reference,args.candidate)))


if __name__ == '__main__':
    main()
