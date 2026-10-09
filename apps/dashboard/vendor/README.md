# Vendored browser dependencies

The web UI is served by a stdlib HTTP server with no build step and no CDN
access, so the 3D scene viewer's dependencies are checked in here and served
from `/vendor/`.

All files are three.js r160, MIT licensed, Copyright © 2010-2024 three.js
authors. Sources under `https://cdn.jsdelivr.net/npm/three@0.160.1/`:

| File | Source path | Why |
| --- | --- | --- |
| `three.module.min.js` | `build/` | the library |
| `OrbitControls.js` | `examples/jsm/controls/` | orbit/zoom/pan on the scene |
| `Line2.js`, `LineGeometry.js`, `LineMaterial.js`, `LineSegments2.js`, `LineSegmentsGeometry.js` | `examples/jsm/lines/` | screen-space thick rays; WebGL ignores `lineWidth` on `LineBasicMaterial` |

**Modification:** the five `lines/` files are stored flat here rather than in a
`lines/` subdirectory, so their `from '../lines/X.js'` imports were rewritten to
`from './X.js'`. Nothing else was changed. The `three` imports resolve through
the import map declared in `index.html`.

To upgrade, download the same two paths at the new version and re-run
`tests/test_sionna_web_ui.py`; the viewer uses only stable core API.
