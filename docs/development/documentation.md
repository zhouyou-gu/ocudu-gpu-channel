# Maintaining documentation

Current documentation is English Markdown. Guides explain how to operate the shipping implementation; reports preserve dated measurements; designs state whether they are proposed, partially implemented, closed or superseded. Korean originals remain labeled historical records.

## Ownership and page structure

| Subject | Canonical owner |
|---|---|
| Vocabulary | Concepts glossary |
| Implemented and measured capabilities | Getting-started status table |
| Configuration, CLI, control and telemetry contracts | Reference |
| Scenario commands | Repository `use_cases/` READMEs |
| Public names and affiliations | Root README contributor table |
| Reproduction, numeric results and evidence | Individual report bundle |

Guides contain a goal, prerequisites, working directory, commands, expected results, cleanup and troubleshooting. Concepts explain the problem and mechanism before details. References define defaults, units, constraints and errors with source links. Reports identify date, revisions, hardware, configuration, method, results and limitations. Designs identify their status and link current implementation or successor material.

Link to canonical definitions instead of copying tables. A capability's implementation state and validation outcome are separate facts. Synthetic success does not qualify live connectivity or strict real-time operation.

## Migration inventory

`docs/_compat/inventory.json` records the pre-migration revision, every source document and evidence asset, SHA-256 hashes, and technical-reference section destinations. `docs/_compat/routes.json` records published file and fragment destinations. Preserve the original evidence hashes in the inventory; declare any sanitized public derivative as described below. Editorial notes may explain later findings without changing historical results.

Preserve old documentation-site routes through generated aliases. The six root milestone stubs and twelve documentation source stubs were retired on 9 October 2026; their old GitHub file URLs are no longer supported. Link directly to canonical documents, including [milestone history](../history/milestones/README.md). Keep `docs/index.html` and the file, fragment and asset mappings; `source_pointers` is empty. The historical migration inventory remains unchanged.

## Public editions

Use portable workspace paths and technical descriptions in public records. Local account names, private endpoints and conversation-specific narration do not establish a result. Retain dates, revisions, authorship, measurements and limitations when editing historical prose; label such edits with a dated note.

Evidence published with normalized path metadata is listed in `docs/_compat/redactions.json`. Each entry retains the inventory’s original SHA-256, the public file’s SHA-256 and the exact JSON fields whose path values changed. Original artifacts are retained in private validation storage. The checker verifies public hashes and all aliases; measurement values and diagnostic patch bytes must remain unchanged. Historical hashes describe the original artifacts, not their public derivatives.

## Publication contract

Markdown is authoritative. Sphinx, MyST and Furo build the site in a separate Python 3.12 environment; runtime packages are unnecessary. Generated HTML stays in ignored `.build/docs/html`. Pull requests validate the artifact; only main or a manual deployment publishes it.

Run the strict build and compatibility checker on the RTX workstation. Review the actual artifact at desktop and mobile widths before publication. Changing the theme or build configuration must preserve legacy routes, readable diagrams and evidence downloads.

## Review checklist

- Define terms before using them; link the glossary.
- Verify commands, defaults and claims against current source and recorded validation.
- Keep one reader task per guide; link deeper reference and evidence.
- Label historical records and open proposals; do not promote old measurements into current qualification.
- Resolve internal links and legacy fragments; review diagrams, tables and code on narrow screens.
- Update current status when qualification changes, and add a concise progress entry linking the detailed report.

## Build and verify

To read the site on any machine, run `scripts/docs/serve.sh` and open `http://127.0.0.1:19492/README.html`. It creates `.venv-docs` with Python 3.12, the same version as CI (fetching it with `uv` when `python3.12` is not installed), runs the strict build into `.build/docs/html` and serves it on loopback; `--build-only` skips serving and `--port N` changes the port. The site needs a build because the Markdown is the source: Sphinx renders it to HTML and generates the navigation, cross-references, search index and legacy routes.

For publication checks, from the repository root on the RTX workstation:

```sh
python3.12 -m venv .venv-docs
.venv-docs/bin/python -m pip install -r docs/requirements.txt
.venv-docs/bin/python -m sphinx -b html -n -W docs .build/docs/html
.venv-docs/bin/python scripts/docs/check_site.py .build/docs/html
```

The checker walks built links and fragments, verifies legacy routing and evidence hashes, and validates the preserved technical-reference SVG drawings, XML and dimensions. It does not establish runtime behavior. Rewritten operational instructions need a separate recorded walkthrough.

For a project-prefix preview, run this from the repository root and leave the server running during review:

```sh
mkdir -p .build/docs/preview
ln -sfn "$PWD/.build/docs/html" .build/docs/preview/ocudu-gpu-channel
python -m http.server 19492 --bind 127.0.0.1 --directory .build/docs/preview
```

Check both `/ocudu-gpu-channel/` and `/ocudu-gpu-channel/index.html#topology`. Unknown fragments must offer navigation rather than redirect to an unrelated section. Stop the server with Ctrl-C. For a browser on another computer, use an SSH local-forward tunnel to this loopback port.

Optional browser review uses Playwright and a Chromium installation, kept outside the pinned build dependencies. In a separate review environment, install `playwright==1.63.0` and its Chromium browser, then serve a preview directory as described above. In a second terminal, against that running server:

```sh
python scripts/docs/check_browser.py \
  --base-url http://127.0.0.1:19492/ocudu-gpu-channel/ \
  --output /tmp/ocudu-docs-browser-review
```

Use `--chromium /path/to/chrome` to reuse an existing browser. The check covers legacy navigation, no-JavaScript fallback, mobile navigation, image loading, horizontal overflow, search exclusions and evidence downloads; screenshots still need visual review. Stop the preview server after review.

To roll back publication, restore the preceding documentation revision through a normal revert and rerun the Pages workflow. The workflow uploads and deploys one complete artifact; a failed build never deploys a partial site.
