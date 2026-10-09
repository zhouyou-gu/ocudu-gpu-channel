# OCUDU GPU Channel documentation

A CUDA channel emulator that applies propagation and interference to IQ samples between live radio stacks. Start with the [current capabilities and measured limits](getting_started/status.md): implementation availability and live qualification are different facts.

## Start here

1. [Understand the project](getting_started/overview.md) and its [key terms](concepts/glossary.md).
2. Check [requirements](getting_started/requirements.md), then [install and build](getting_started/installation.md).
3. Follow the [first synthetic CUDA run](getting_started/first-run.md).
4. Choose a [use case](use_cases/README.md) or continue to [live stacks](guides/live-stacks.md), [Sionna](guides/sionna.md) and the [dashboard](guides/dashboard.md).

## Other reading paths

| Reader | Path |
|---|---|
| Developer | [Architecture](concepts/architecture.md) → [project structure](development/project-structure.md) → [reference](reference/README.md) → [testing](development/testing.md) → [profiling](development/profiling.md) |
| Researcher or evaluator | [Status](getting_started/status.md) → [reports](reports/README.md) → the report's method, setup, evidence and limitations |
| Returning contributor | [Open designs](development/designs/README.md) and [development history](history/README.md) |

Current guidance is English. Historical records retain their original language and revision-specific claims. The [repository README](../README.md#contributors) owns contributor information.

```{toctree}
:maxdepth: 2
:hidden:
:caption: Documentation

getting_started/README
concepts/README
guides/README
reference/README
use_cases/README
development/README
reports/README
history/README
```
