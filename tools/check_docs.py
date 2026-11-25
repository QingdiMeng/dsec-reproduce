"""Validate paired documentation, language navigation and local file links offline."""
import argparse
import json
import re
from html import unescape
from pathlib import Path
from urllib.parse import unquote, urlsplit


def editions(root):
    catalog = json.loads((root / 'docs/languages.json').read_text())
    pairs = {}
    for primary, names in catalog.items():
        if primary not in {'en', 'zh-CN'}:
            raise ValueError(f'Unknown documentation language {primary}')
        other = 'en' if primary == 'zh-CN' else 'zh-CN'
        for name in names:
            path = Path(name)
            if path in pairs or path.suffix != '.md' or path.is_absolute() or '..' in path.parts:
                raise ValueError(f'Invalid or duplicate documentation entry {path}')
            pairs[path] = {primary: path, other: path.with_suffix(f'.{other}.md')}
    return pairs


FENCE = re.compile(r'(?ms)^[ \t]*(`{3,}|~{3,})([^\n]*)\n(.*?)^[ \t]*\1[ \t]*$')
EXAMPLE_LANGUAGES = {'sh', 'bash', 'python', 'json'}


def examples(page):
    return [(lang, body) for _, lang, body in FENCE.findall(page) if lang in EXAMPLE_LANGUAGES]


def prose(page):
    return FENCE.sub('', page)


def anchors(page):
    """Collect explicit anchors and GitHub-style slugs for repository headings."""
    ids = set(re.findall(r'<a\s+(?:name|id)=["\']([^"\']+)["\']', page))
    counts = {}
    for title in re.findall(r'^#{1,6}\s+(.+)$', page, re.M):
        title = re.sub(r'\[([^]]+)\]\([^)]+\)', r'\1', title)
        title = unescape(re.sub(r'<[^>]+>', '', title)).lower()
        slug = re.sub(r'[^\w -]', '', title).strip().replace(' ', '-')
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        ids.add(f'{slug}-{count}' if count else slug)
    return ids


def check(root):
    root = Path(root).resolve(strict=True)
    pairs = editions(root)
    errors = []
    languages = {path: lang for pair in pairs.values() for lang, path in pair.items()}
    sections = {}
    for pair in pairs.values():
        samples = {}
        for lang, relative in pair.items():
            source = root / relative
            if not source.is_file():
                errors.append(f'Missing {lang} documentation: {relative}')
                continue
            other = pair['en' if lang == 'zh-CN' else 'zh-CN']
            page = source.read_text()
            samples[lang] = examples(page)
            text = prose(page)
            if f']({other.name})' not in text:
                errors.append(f'{relative}: missing link to {other.name}')
            for target in re.findall(r'\[[^\]\n]*\]\(([^)\n]+)\)', text):
                link = urlsplit(target.strip('<>'))
                if link.scheme or link.netloc:
                    continue
                destination = (source.parent / unquote(link.path)).resolve() if link.path else source
                if not destination.is_relative_to(root) or not destination.exists():
                    errors.append(f'{relative}: missing {target}')
                    continue
                dest = destination.relative_to(root)
                if link.fragment and destination.suffix == '.md':
                    if dest not in sections:
                        sections[dest] = anchors(prose(destination.read_text()))
                    if unquote(link.fragment) not in sections[dest]:
                        errors.append(f'{relative}: missing section {target}')
                if dest in languages and languages[dest] != lang and dest != other:
                    errors.append(f'{relative}: cross-language link {target}')
        if len(samples) == 2 and samples['en'] != samples['zh-CN']:
            errors.append(f"{pair['en']} and {pair['zh-CN']}: code examples differ")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    errors = check(args.project)
    if errors:
        print('\n'.join(errors))
        raise SystemExit(1)
    print('Documentation pairs, code examples, local links and sections passed')


if __name__ == '__main__':
    main()
