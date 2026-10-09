"""Standalone documentation build; never import project runtime packages."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'docs'))
project = 'OCUDU GPU Channel'
author = 'OCUDU GPU Channel contributors'
copyright = '2026, OCUDU GPU Channel contributors'
extensions = ['myst_parser', 'site_support']
root_doc = 'README'
source_suffix = {'.md': 'markdown'}
exclude_patterns = ['_compat/**', 'requirements.txt', '**/.DS_Store']
myst_enable_extensions = ['colon_fence', 'deflist', 'dollarmath', 'attrs_inline']
myst_heading_anchors = 6
html_theme = 'furo'
html_title = 'OCUDU GPU Channel'
html_static_path = ['assets']
html_css_files = ['legacy-diagrams.css', 'docs.css']
html_favicon = 'assets/branding/logo.svg'
html_logo = 'assets/branding/logo.svg'
html_baseurl = 'https://zhouyou-gu.github.io/ocudu-gpu-channel/'
html_theme_options = {'navigation_with_keys': True, 'sidebar_hide_name': False}
html_show_sourcelink = False
html_copy_source = False
html_use_index = False
