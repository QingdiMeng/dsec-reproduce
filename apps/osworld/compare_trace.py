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


def compare(reference, candidate):
    sources = [list(map(json.loads, (d/'trace.jsonl').read_text().splitlines()))
               for d in (reference, candidate)]
    if len(sources[0]) != len(sources[1]) or any(
            (lhs['step'], lhs['action']) != (rhs['step'], rhs['action'])
            for lhs, rhs in zip(*sources)):
        raise ValueError('Replay inputs do not match')
    checkpoints = [(lhs, rhs) for lhs, rhs in zip(*sources) if 'roi' in lhs['action']]
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reference',type=Path)
    parser.add_argument('candidate',type=Path)
    args=parser.parse_args()
    print(json.dumps(compare(args.reference,args.candidate)))


if __name__ == '__main__':
    main()
