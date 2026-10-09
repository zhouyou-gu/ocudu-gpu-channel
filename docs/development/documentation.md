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

`docs/_compat/inventory.json` records the pre-migration revision, every source document and evidence asset, SHA-256 hashes, and technical-reference section destinations. `docs/_compat/routes.json` records published file and fragment destinations. Evidence hashes must remain unchanged; editorial notes may explain later findings without changing historical results.

The approved sequence is inventory, publishing foundation, current guidance, reports/history, and validation. Each stage is a separate commit. Preserve old public routes through generated aliases; keep only short forwarding files for explicitly published repository document paths. Root milestone filenames remain forwarding entrypoints.

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
