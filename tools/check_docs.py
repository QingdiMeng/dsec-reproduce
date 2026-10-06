"""Check repository Markdown file links without network access."""
import argparse
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


def check(root):
    root = Path(root).resolve(strict=True)
    errors = []
    files = [root / 'README.md', root / 'CONTRIBUTING.md', root / 'ROADMAP.md',
             root / 'THIRD_PARTY_NOTICES.md', *sorted((root / 'docs').rglob('*.md')),
             *sorted((root / 'apps').rglob('README.md'))]
    for source in files:
        text = re.sub(r'```.*?```', '', source.read_text(), flags=re.S)
        for target in re.findall(r'\[[^\]\n]+\]\(([^)\n]+)\)', text):
            link = urlsplit(target.strip('<>'))
            if link.scheme or link.netloc or not link.path:
                continue
            destination = (source.parent / unquote(link.path)).resolve()
            if not destination.is_relative_to(root) or not destination.exists():
                errors.append(f'{source.relative_to(root)}: missing {target}')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    errors = check(args.project.resolve())
    if errors:
        print('\n'.join(errors))
        raise SystemExit(1)
    print('Documentation file links passed')


if __name__ == '__main__':
    main()
