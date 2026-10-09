> Editorial note (2026-10-09): local paths and workflow narration were normalized for this [public edition](../../development/documentation.md#public-editions); measurements and conclusions are unchanged.

# Documentation migration validation — 2026-10-09

## Scope and revisions

This migration replaces the large HTML reference with an English Markdown documentation site, separates current guidance from measured reports and history, and preserves legacy publication routes. It changes documentation and publishing infrastructure, not runtime interfaces or qualification criteria.

The inventory baseline is `58d3156c939187580a723db29e4beeb667b648f9`. The first four documentation stages are `2ef02a7` (inventory), `952814f` (publishing), `ccea50c` (current pages) and `5a7476b` (reports/history). This report belongs to the fifth, validation stage. Runtime builds use the same implementation as the inventory baseline; subsequent source edits are documentation references or comments.

Validation ran on the RTX workstation in `~/ocudu-gpu-channel-workspace/validation/docs-20261009/`, using an isolated tracked-tree mirror. Hardware: NVIDIA GeForce RTX 5090, Intel Core Ultra 9 285K, 24 logical CPUs. Software: Linux `6.8.0-137-lowlatency`, NVIDIA driver `580.173.02`, Python `3.12.3`, Node `22.20.0`, and Playwright `1.63.0` with the workstation’s existing Chromium executable. The documentation environment uses the exact pins in `docs/requirements.txt`; it contains no Sionna, CUDA Python, dashboard, bridge, NumPy or Torch runtime.

## Publication checks

| Check | Result and boundary |
|---|---|
| Strict Sphinx build | Passed with `-n -W`; no unresolved document references, missing assets or duplicate labels |
| Migration accounting | All 125 inventoried source files and 34 major technical-reference sections have destinations |
| Legacy routing | All 82 original technical-reference IDs retained; 114 explicit fragment routes tested across original HTML pages |
| Published files and evidence | 117 file mappings and 18 tracked source forwarding files checked; all 71 original evidence assets and their download aliases retain recorded SHA-256 hashes |
| Technical-reference diagrams | All 22 inline SVG drawings retained; drawing-body hashes, XML validity and case-sensitive `viewBox` attributes checked |
| Browser behavior | Project prefix `/ocudu-gpu-channel/`, root/index entry, known and unknown hashes, case-sensitive paths, and JavaScript-disabled navigation checked |
| Responsive review | Landing page, tutorial, architecture, glossary, channel equations, configuration, control API, backends and measured boundaries reviewed at 1440 px and 390 px; no horizontal page overflow |
| Navigation and search | Mobile navigation and full-size diagram links work; setup and control queries find current pages; historical records are excluded from default search |
| Interface traceability | CLI reference exactly matches the source-derived generator; configuration, scene resolution, control acknowledgment/application and telemetry semantics reviewed against source |
| Fresh-reader review | Setup, reference rules, measured limits and proposed work were findable from the documentation alone; identified gaps were corrected |

The SVG extraction initially exposed case-sensitive attribute and HTML-entity issues. The final checker validates standalone XML and preserved drawing bodies, so a counted but broken image cannot pass. Mobile review also led to stacked architecture cells, scrollable wide tables and full-size SVG links. Historical measurements and diagram contents were preserved.

## Operational walkthroughs

Fresh CPU and CUDA builds each passed **12/12 CTest tests**. The documented first-run Bash block was executed with both sources and both sinks running concurrently; only owned processes were stopped afterward. Separate interrupt and termination probes verified that the tutorial exits and releases its children and four ports.

| Backend / configuration | Source pulls | Receive requests | Sink samples | Average sink power |
|---|---:|---:|---|---|
| CUDA, `topology.mvp.cuda.yaml` | 16,054 | 16,000 | 184,320,000 per sink | 0.501187 per sink |
| CPU reference, `topology.local.cpu.yaml` | 16,050 | 15,996 | 184,227,840 and 184,320,000 | 1 per sink |

Both used 23.04 MS/s and 23,040-sample blocks, with two directed edges. Sources ran for at most 15 seconds, the broker for 12 seconds, and sinks for 8 seconds. Both runs recorded zero receive starvations, queue overflows, sequence gaps and ZMQ errors. Their different topology gains explain the different output powers; this is not a CPU/CUDA parity comparison.

The documented single-cell Sionna launcher completed an eight-second synthetic scene run using the workstation's existing Sionna environment. The bridge produced **177 committed batches**, **354 applied updates**, zero rejected updates and nonzero IQ traffic; broker integrity counters were zero. HTTP readiness succeeded and owned processes stopped. This verifies the documented snapshot-update workflow, not a buffered CIR proposal or live radio movement.

A fresh installation of `.[dashboard]` served the dashboard from `/tmp`, outside the repository. Both installed console entrypoints returned help; `/healthz` returned success and the dashboard returned its complete browser page. With no broker attached, health correctly reported disconnected feeds. The dashboard extra did not install Sionna.

The fixed-matrix model fragment in the channel-model guide was combined with the existing two-port topology and processed successfully by the CUDA benchmark: 200 model-mix calls. This 50 ms configuration smoke reported a red latency band; it is a syntax/processing check, not performance qualification. Correlation and LOS declaration rules were checked against the parser and validator.

Python validation passed **211 tests and 4,250 subtests**, with three skips: a network-namespace test needs `CAP_SYS_ADMIN`, MuJoCo offscreen rendering lacks an OpenGL context, and the scheduler fixture needs its pinned external OCUDU tree. Both frontend disconnect and RAN grouping/recovery checks passed. Skips are not passes.

## Reproduction and evidence

Use the [installation](../../getting_started/installation.md), [first-run tutorial](../../getting_started/first-run.md), [Sionna guide](../../guides/sionna.md) and [testing guide](../../development/testing.md) for the commands and cleanup. The two tutorial configurations are under `use_cases/configs/topologies/basic/`. Set `OCUDU_SIONNA_DEMO_DURATION_SECONDS=8` and select the existing compatible Sionna interpreter to reproduce the short scene check.

From the repository root in the pinned documentation environment:

```sh
python -m sphinx -b html -n -W docs .build/docs/html
python scripts/docs/check_site.py .build/docs/html
```

For browser checks, install the optional Playwright review tool, serve the actual generated artifact under the project prefix, and run `scripts/docs/check_browser.py` as described in [maintaining documentation](../../development/documentation.md). The preview uses the same output directory uploaded by the Pages workflow.

The remote evidence directory retains `build.log`, `check.json`, `browser.log`, `screenshots/browser.json`, desktop/mobile screenshots, CPU/CUDA configure/build/CTest logs, `first-run.json`, tutorial/interrupt logs, `cleanup-check.json`, `configuration-walkthrough.log`, `pytest.log`, both frontend logs, package/help logs, `dashboard-check.json`, and the `sionna-walkthrough/` broker, bridge and sink records. Original report evidence is separately protected by `docs/_compat/inventory.json`; it was not regenerated.

## Limits and publication

These checks establish documentation build integrity and the exercised synthetic commands. They do not qualify continuous moving traffic, strict live real-time operation, OAI, CMX/X310, GB10 or Orin. The prior [structure-migration report](structure-migration-20261009.md) remains the authority for its nine-stage GPU sequence, deterministic IQ comparison and targeted live run, including its two receive starvations.

The generated artifact was previewed after the five migration stages. Validation did not deploy the site. Pull requests build and validate without deployment; main and manual main-branch runs deploy the complete artifact. Publication rollback is a normal revert of the documentation migration followed by the Pages workflow, preserving the prior revision and contributor history.
