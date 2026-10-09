"""Repository links, historical metadata and explicit legacy URL aliases."""
from pathlib import Path
import html
import json
import posixpath
import re
import shutil
from urllib.parse import quote, urlsplit, unquote

REPOSITORY = 'https://github.com/zhouyou-gu/ocudu-gpu-channel/blob/main/'


def prepare_source(app, docname, source):
    """Keep source-relative repository links useful in the published site."""
    docs = Path(app.srcdir)
    here = docs / (docname + '.md')
    text = source[0]
    if docname.startswith('history/') and not text.startswith('---\n'):
        text = '---\nno-search: true\n---\n\n' + text

    def rewrite(match):
        prefix, url, suffix = match.groups()
        parts = urlsplit(html.unescape(url))
        if parts.scheme or parts.netloc or not parts.path:
            return match.group(0)
        target = (here.parent / unquote(parts.path)).resolve()
        # MyST's path#fragment resolver handles heading slugs; explicit labels
        # must use its global-reference form instead.
        if target.suffix == '.md' and target.is_file() and parts.fragment:
            marker = '(' + unquote(parts.fragment) + ')='
            if marker in target.read_text(encoding='utf-8').splitlines():
                return prefix + unquote(parts.fragment) + suffix
        root = docs.parent.resolve()
        if target.is_relative_to(root) and not target.is_relative_to(docs.resolve()):
            path = target.relative_to(root).as_posix()
            if target.is_dir():
                url = REPOSITORY.replace('/blob/', '/tree/') + quote(path)
            else:
                url = REPOSITORY + quote(path)
            if parts.fragment:
                url += '#' + parts.fragment
            return prefix + url + suffix
        return match.group(0)

    text = re.sub(r'(\]\()([^\s)]+)(\))', rewrite, text)
    text = re.sub(r'((?:href|src)=["\'])([^"\']+)(["\'])', rewrite, text)
    # Historical HTML is a standalone page, not a relocated download: its
    # relative links must retain their canonical directory.
    def standalone(match):
        label, url = match.groups()
        if (here.parent / urlsplit(url).path).is_file():
            return '<a href="' + html.escape(url, quote=True) + '">' + html.escape(label) + '</a>'
        return match.group(0)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+\.html(?:#[^)]*)?)\)', standalone, text)
    source[0] = text


def redirect_page(destination, fragments=None, title='Documentation moved'):
    """No immediate refresh: the original fragment must be read first."""
    options = fragments or {}
    payload = json.dumps({'default': destination, 'fragments': options}).replace('<', '\\u003c')
    links = ''.join(f'<li><a href="{html.escape(value, quote=True)}">{html.escape(key)}</a></li>'
                    for key, value in options.items())
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title><link rel="canonical" href="{html.escape(destination, quote=True)}">
<style>body{{max-width:58rem;margin:3rem auto;padding:0 1rem;font:16px/1.6 system-ui}}a{{color:#1765a3}}</style></head>
<body><h1>{html.escape(title)}</h1><p id="message">This documentation has a new structure.</p>
<p><a href="{html.escape(destination, quote=True)}">Open the current documentation</a></p>
<p><a href="{html.escape(options.get('key-terms', destination), quote=True)}">Key terms and glossary</a></p>
<details><summary>Legacy section destinations</summary><ul>{links}</ul></details>
<noscript><p>JavaScript is disabled. Use the links above to choose a section.</p></noscript>
<script type="application/json" id="routes">{payload}</script>
<script>
const routes = JSON.parse(document.getElementById('routes').textContent);
let fragment;
try {{ fragment = decodeURIComponent(location.hash.slice(1)); }} catch (_) {{ fragment = location.hash.slice(1); }}
if (!fragment) location.replace(routes.default);
else if (Object.prototype.hasOwnProperty.call(routes.fragments, fragment)) location.replace(routes.fragments[fragment]);
else document.getElementById('message').textContent = 'Unknown legacy section: ' + fragment + '. Choose the current documentation or a section below.';
</script></body></html>'''


def finish_site(app, exception):
    if exception or app.builder.format != 'html':
        return
    docs, out = Path(app.srcdir), Path(app.outdir)
    routes = json.loads((docs / '_compat/routes.json').read_text())
    # Retain canonical downloadable evidence and standalone historical HTML.
    for path in docs.rglob('*'):
        relative = path.relative_to(docs)
        if not path.is_file() or relative.parts[0] in {'_compat', '__pycache__'}:
            continue
        if path.suffix in {'.json', '.csv', '.png', '.svg', '.patch', '.txt', '.html'} and path.name not in {'requirements.txt', 'index.html'}:
            destination = out / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
    for old, new in routes['assets'].items():
        destination = out / old
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(docs / new, destination)
    mappings = dict(routes['files'])
    mappings['index.html'] = routes['root']
    for old, new in mappings.items():
        if old in routes['assets'] or old == new:
            continue
        destination = out / old
        destination.parent.mkdir(parents=True, exist_ok=True)
        relative = posixpath.relpath(new, posixpath.dirname(old) or '.')
        fragments = {key.split('#', 1)[1]: posixpath.relpath(value.split('#',1)[0], posixpath.dirname(old) or '.') + '#' + value.split('#',1)[1]
                     for key, value in routes['fragments'].items() if key.startswith(old + '#')}
        if old.endswith('.md'):
            destination.write_text(f'# Documentation moved\n\nContinue at [{new}]({relative}).\n', encoding='utf-8')
        elif old.endswith('.html'):
            # A canonical historical HTML record must never be overwritten by itself.
            destination.write_text(redirect_page(relative, fragments), encoding='utf-8')


def setup(app):
    app.connect('source-read', prepare_source)
    app.connect('build-finished', finish_site)
    return {'version': '1', 'parallel_read_safe': True, 'parallel_write_safe': True}
