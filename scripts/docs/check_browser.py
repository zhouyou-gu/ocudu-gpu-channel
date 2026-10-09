#!/usr/bin/env python3
"""Optional Playwright review against a served GitHub Pages artifact prefix."""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urljoin, unquote
from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--base-url', required=True, help='Full project prefix, ending in /')
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--chromium', help='Optional existing Chromium executable')
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
repo = Path(__file__).resolve().parents[2]
routes = json.loads((repo/'docs/_compat/routes.json').read_text())
review_pages = ['README.html','getting_started/first-run.html','concepts/architecture.html',
                'concepts/glossary.html','concepts/channel-models.html','reference/configuration.html','reference/control-api.html',
                'reference/backends.html','reports/performance/measured-boundaries.html']
results = {'legacy_fragments':0,'views':[],'search':{}}
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path=args.chromium)
    page = browser.new_page()
    for old, destination in routes['fragments'].items():
        page.goto(urljoin(args.base_url, old), wait_until='domcontentloaded')
        page.wait_for_url(urljoin(args.base_url, destination))
        target = unquote(destination.split('#',1)[1])
        assert page.locator('[id="'+target+'"]').count() == 1, old
        results['legacy_fragments'] += 1
    for entry in ['', 'index.html']:
        page.goto(urljoin(args.base_url,entry))
        page.wait_for_url(urljoin(args.base_url,'README.html'))
    page.goto(urljoin(args.base_url,'index.html#unknown-legacy-section'))
    assert page.locator('#message').inner_text().startswith('Unknown legacy section:')
    no_js = browser.new_context(java_script_enabled=False)
    fallback = no_js.new_page();fallback.goto(urljoin(args.base_url,'index.html#topology'))
    assert fallback.get_by_text('Open the current documentation').count() == 1
    assert fallback.get_by_text('Open the current documentation').is_visible()
    assert '<noscript>' in fallback.content()
    no_js.close()
    for width,height in [(1440,1000),(390,844)]:
        page.set_viewport_size({'width':width,'height':height})
        for target in review_pages:
            response=page.goto(urljoin(args.base_url,target),wait_until='networkidle')
            assert response.status==200
            assert page.locator('h1').count()>=1
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'),(target,width,'horizontal page overflow')
            assert page.locator('article img').evaluate_all('(imgs) => imgs.every(i => i.complete && i.naturalWidth > 0)'),target
            screenshot=args.output/(target.replace('/','-').replace('.html','')+'-'+str(width)+'.png')
            page.screenshot(path=str(screenshot),full_page=True)
            results['views'].append({'page':target,'width':width,'screenshot':screenshot.name})
            if width == 390 and target == 'README.html':
                page.locator('label[for="__navigation"]:visible').first.click()
                assert page.locator('.sidebar-drawer').is_visible()
                page.locator('.sidebar-drawer a').filter(has_text='Getting started').first.click()
                page.wait_for_url(urljoin(args.base_url,'getting_started/README.html'))
            if target == 'reference/backends.html':
                page.get_by_text('Open this diagram at full size').first.click()
                assert page.url.endswith('.svg'),page.url
    page.set_viewport_size({'width':1440,'height':1000})
    for query in ['first run','matrix_profile_swap','runtime-mutable-channel']:
        page.goto(urljoin(args.base_url,'search.html?q='+query))
        page.wait_for_function("!document.querySelector('#search-results .search-summary') || !document.querySelector('#search-results .search-summary').textContent.includes('Searching')")
        page.wait_for_timeout(1000)
        links=page.locator('#search-results li a').evaluate_all('(links) => links.map(a => ({title:a.textContent, href:a.getAttribute("href")}))')
        assert not any('/history/' in '/'+item['href'] for item in links),(query,links)
        if query in ['first run','matrix_profile_swap']:
            assert links,(query,'no search results')
        results['search'][query]=links
    # Test old downloads through HTTP, not only the on-disk alias.
    for old,new in routes['assets'].items():
        response=page.request.get(urljoin(args.base_url,old))
        assert response.status==200,old
        assert hashlib.sha256(response.body()).digest()==hashlib.sha256((repo/'docs'/new).read_bytes()).digest(),old
    browser.close()
(args.output/'browser.json').write_text(json.dumps(results,indent=2)+'\n')
print(json.dumps({'legacy_fragments':results['legacy_fragments'],'views':len(results['views']),'search_queries':len(results['search']),'asset_aliases':len(routes['assets'])}))
