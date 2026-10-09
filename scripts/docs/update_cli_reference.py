#!/usr/bin/env python3
"""Regenerate CLI declarations from source without importing runtime packages."""
import ast
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]


def render():
    lines = ['# Command-line interfaces', '',
             'Generated from shipping entrypoint declarations by `scripts/docs/update_cli_reference.py`. Run commands from the repository root unless input paths are absolute. This reference imports no runtime packages.', '',
             '## Broker defaults and precedence', '',
             '`--config` is required. `--duration` defaults to zero (run until stopped); durations accept the units parsed by `app_support.h`. Control and telemetry endpoints default to disabled; telemetry requires a control server. `--telemetry-rate-hz` defaults to 20 and `--control-warmup-cap-slots` to 3. Strict real-time and strict hardware checks are opt-in. Wire capture defaults to disabled: provide both directory and sample count, with skip defaulting to zero.', '',
             'The broker does not overlay environment variables onto YAML. Orchestration scripts may choose files and pass CLI options; their variables belong to the owning guide. Repeated parsed CLI values use the last assignment unless the option is explicitly repeatable. Unknown or incomplete broker arguments exit with status 2; runtime failures exit with status 1.', '',
             'Usage examples below show syntax, not a universal default for every bracketed value. See [configuration](configuration.md), [control](control-api.md), and [telemetry](telemetry.md) for the contracts.', '']
    for path in sorted((ROOT / 'apps').glob('ocudu_*.cpp')):
        text = path.read_text()
        match = re.search(r'void usage\(\)\s*\{(.*?)\n\}', text, re.S)
        if not match:
            continue
        usage = ''.join(ast.literal_eval(token) for token in re.findall(r'"(?:[^"\\]|\\.)*"', match[1]))
        name = path.stem.replace('_','-')
        lines += ['## ' + name, '', f'[Source](../../{path.relative_to(ROOT).as_posix()})', '', '```text', usage.rstrip(), '```', '']
    lines += ['## ocudu-gpu-hog', '', '[Source](../../apps/ocudu_gpu_hog.cu). Controlled GPU contention tool; it does not run a radio.', '',
              '```text', 'ocudu-gpu-hog [--duration-s S] [--kernel-us US] [--duty 0..1] [--blocks N] [--threads N] [--streams S] [--queue-depth K]', '```','']
    for filename, command in [('apps/sionna_bridge/run_bridge.py','ocudu-sionna-bridge'),('apps/dashboard/server.py','ocudu-dashboard')]:
        text = (ROOT / filename).read_text(); tree = ast.parse(text)
        constants = {}
        for item in tree.body:
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target,ast.Name):constants[target.id]=item.value
        def value(node):
            if isinstance(node,ast.Name) and node.id in constants:
                node=constants[node.id]
            try:return repr(ast.literal_eval(node))
            except (ValueError,TypeError):return ast.unparse(node)
        lines += ['## '+command,'',f'[Source](../../{filename}). Python declaration defaults are shown below; scenario overrides are described in [configuration](configuration.md#scenario-paths).','',
                  '| Option | Default | Meaning |','|---|---|---|']
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='parse_args')
        for node in ast.walk(fn):
            if not isinstance(node,ast.Call) or not isinstance(node.func,ast.Attribute) or node.func.attr!='add_argument':continue
            flags=[a.value for a in node.args if isinstance(a,ast.Constant) and isinstance(a.value,str)]
            if not flags:continue
            kw={k.arg:k.value for k in node.keywords}
            default=value(kw['default']) if 'default' in kw else ('False' if kw.get('action') and value(kw['action'])=="'store_true'" else 'None')
            help_text=value(kw['help']).strip("'") if 'help' in kw else 'See option name and source declaration.'
            if 'choices' in kw:help_text+=' Choices: '+value(kw['choices'])+'.'
            if kw.get('action') and ast.unparse(kw['action'])=='argparse.BooleanOptionalAction':flags.append('--no-'+flags[0][2:])
            lines += ['| '+', '.join('`'+f+'`' for f in flags)+' | `'+default.replace('|','\\|')+'` | '+help_text.replace('|','\\|').replace('\n',' ')+' |']
        if command=='ocudu-sionna-bridge':
            lines += ['', 'Propagation flags use paired `--NAME` / `--no-NAME` forms: `los` and `specular-reflection` default to true; `diffuse-reflection`, `refraction` and `diffraction` default to false. `--hold-until-file` requires grid mode; grid mode rejects live position input. Control fanout endpoints must be distinct and position timeout must be positive.']
        lines += ['']
    lines += ['## Supporting tools and launchers','',
              'Scene construction, coverage measurement, topology checking and profile replay live with the [Sionna application](../../apps/sionna_bridge/). Use each script’s `--help` for its task-specific arguments. Hardware bridge options are owned by the [USRP integration](../../use_cases/cmx500/README.md).', '',
              'Workspace preparation and live gate variables are documented by the [native scripts](../../scripts/native/README.md), [remote scripts](../../scripts/remote/README.md), and each [use case](../use_cases/README.md). The [Sionna](../guides/sionna.md) and [dashboard](../guides/dashboard.md) guides show supported launch combinations.', '']
    return '\n'.join(lines)


if __name__ == '__main__':
    (ROOT / 'docs/reference/cli.md').write_text(render())
