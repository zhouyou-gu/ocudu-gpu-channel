#!/usr/bin/env python3
"""Check the built artifact, legacy destinations and immutable evidence bytes."""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit
from bs4 import BeautifulSoup


def check(site, repo):
    docs = repo / 'docs'
    routes = json.loads((docs / '_compat/routes.json').read_text())
    inventory = json.loads((docs / '_compat/inventory.json').read_text())
    errors, ids, documents = [], {}, {}
    for file in site.rglob('*.html'):
        soup = BeautifulSoup(file.read_text(encoding='utf-8'), 'html.parser')
        documents[file.resolve()] = soup
        values = [tag['id'] for tag in soup.select('[id]')]
        ids[file.resolve()] = set(values)
        duplicates = {value for value in values if values.count(value) > 1}
        if duplicates:
            errors.append(f'{file.relative_to(site)}: duplicate IDs {sorted(duplicates)}')

    def target_exists(origin, url):
        parsed = urlsplit(url)
        if parsed.scheme or parsed.netloc or not parsed.path and not parsed.fragment:
            return
        path = unquote(parsed.path)
        if path.startswith('/ocudu-gpu-channel/'):
            target = site / path[len('/ocudu-gpu-channel/'):]
        elif path.startswith('/'):
            target = site / path.lstrip('/')
        else:
            target = origin.parent / path if path else origin
        target = target.resolve()
        if target.is_dir():
            target /= 'index.html'
        if not target.exists():
            errors.append(f'{origin.relative_to(site)}: missing {url}')
        elif parsed.fragment and target.suffix == '.html' and unquote(parsed.fragment) not in ids.get(target, set()):
            errors.append(f'{origin.relative_to(site)}: missing fragment {url}')

    links = 0
    for file, soup in documents.items():
        for tag in soup.select('a[href], img[src], script[src], link[href], iframe[src]'):
            url = tag.get('href', tag.get('src'))
            target_exists(file, url)
            links += 1
    for old, destination in routes['files'].items():
        if not (site / old).is_file():
            errors.append(f'legacy file missing: {old}')
        target_exists(site / 'index.html', destination)
    for old, destination in routes['fragments'].items():
        target_exists(site / 'index.html', destination)
        page, fragment = old.split('#', 1)
        soup = documents.get((site / page).resolve())
        embedded = soup.find('script', id='routes') if soup else None
        if embedded:
            actual = json.loads(embedded.string)['fragments'].get(fragment)
            expected = __import__('posixpath').relpath(destination.split('#',1)[0], str(Path(page).parent)) + '#' + destination.split('#',1)[1]
            if actual != expected:
                errors.append(f'legacy fragment misrouted: {old}: {actual!r}, expected {expected!r}')
        elif fragment not in ids.get((site / page).resolve(), set()):
            errors.append(f'legacy fragment lost: {old}')
    evidence = 0
    for record in inventory['files']:
        destination = repo / record['destination']
        if not destination.exists():
            errors.append(f'inventory destination missing: {record["destination"]}')
        if record['kind'] != 'evidence':
            continue
        evidence += 1
        if not destination.exists() or hashlib.sha256(destination.read_bytes()).hexdigest() != record['sha256']:
            errors.append(f'evidence changed: {record["source"]}')
        old = record['source'].removeprefix('docs/')
        legacy = site / old
        if not legacy.exists() or hashlib.sha256(legacy.read_bytes()).hexdigest() != record['sha256']:
            errors.append(f'published evidence changed: {old}')
    diagrams = list((docs / 'assets/diagrams').glob('reference-*.svg'))
    if len(diagrams) != inventory['inline_svg_count']:
        errors.append(f'expected {inventory["inline_svg_count"]} technical-reference SVGs; found {len(diagrams)}')
    print(json.dumps({'html_pages':len(documents),'links':links,'legacy_fragments':len(routes['fragments']),
                      'evidence_files':evidence,'reference_diagrams':len(diagrams),'errors':errors}, indent=2))
    return bool(errors)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('site', type=Path)
    args = parser.parse_args()
    raise SystemExit(check(args.site.resolve(), Path(__file__).resolve().parents[2]))
