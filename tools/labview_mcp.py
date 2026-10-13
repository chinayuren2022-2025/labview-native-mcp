"""LabVIEW 2021 MCP: native editing, configuration, execution and image export.

All COM references stay on one STA thread. Refnums are session-local and must be
rediscovered after deleting/replacing objects or reverting a VI. Configure nodes
before discovering their terminals; mode changes can invalidate old terminals.

Launch with Python312/python.exe tools/labview_mcp.py (stdio). Client command must
use the actual Python installation, not WindowsApps/python.exe. Requires installed
LabVIEW 2021, pywin32, mcp and the patched work/lvscript library. This service does
not register itself in an AI client or modify client configuration.
Verified through an independent MCP client: CRC construction/execution and PNG,
control direction changes, Build Array resizing, Case frame insertion/editing,
and PropertyItem.IsWrite transitions. Polymorphic U32->U16 selection and edited
output execution passed. Express Simulate Signal mapped Amplitude persistence
passed; regeneration-dependent configuration (signal kind/terminal changes) is
not implemented or verified by configure_express_parameters.
The real mcp_boundary.py client also exercises error recovery, overwrite refusal,
scalar/U8-array/cluster shapes, concurrent requests, and two nested For Loops.
COM byte SAFEARRAY results are recursively normalized to JSON arrays on the STA.
These finite cases do not guarantee arbitrary nodes, modes or data types.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pythoncom
import win32com.client
from mcp.server.fastmcp import FastMCP, Image
from lvscript import Session, ROOT, cluster_array

mcp = FastMCP('LabVIEW Native Scripting')
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='labview-sta')
_session = None


def _dispatch(operation, args):
    global _session
    if _session is None:
        pythoncom.CoInitialize()
        _session = Session()
    return encode(operation(_session, *args))


async def native(operation, *args):
    return await asyncio.get_running_loop().run_in_executor(_executor, _dispatch, operation, args)


def encode(value):
    """Keep COM byte SAFEARRAYs and nested clusters JSON-shaped, not repr strings."""
    if isinstance(value, (memoryview, bytes, bytearray)):
        return list(value)
    if isinstance(value, (tuple, list)):
        return [encode(item) for item in value]
    if isinstance(value, dict):
        return {key: encode(item) for key, item in value.items()}
    return value


def decode(value):
    """Explicit JSON to COM types; clusters must be tuples, not SAFEARRAY rows."""
    if isinstance(value, dict) and 'type' in value:
        kind, data = value['type'], value['value']
        if kind == 'cluster':
            return tuple(decode(v) for v in data)
        if kind == 'cluster_array':
            return cluster_array([tuple(decode(v) for v in row) for row in data])
        types = {'u8_array': pythoncom.VT_ARRAY | pythoncom.VT_UI1,
                 'u16': pythoncom.VT_UI2, 'u32': pythoncom.VT_UI4,
                 'i32': pythoncom.VT_I4, 'double': pythoncom.VT_R8}
        if kind not in types:
            raise ValueError(f'Unsupported COM type: {kind}')
        return win32com.client.VARIANT(types[kind], bytes(data) if kind == 'u8_array' else data)
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value


@mcp.tool()
async def new_vi(path: str) -> dict:
    """Create an empty VI. Refuse existing paths; do not overwrite user work."""
    def create(s):
        p = Path(path).resolve()
        if p.exists():
            raise FileExistsError(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        s.new_vi(p)
        return {'path': str(p), 'diagram': s.diagram(p), 'panel': s.prop_get('VI', 'Panel', s.vi(p))}
    return await native(create)


@mcp.tool()
async def open_vi(path: str, show: bool = False) -> dict:
    """Open an existing VI; optionally show its front panel and block diagram."""
    def open_(s):
        p = Path(path).resolve()
        ref = s.vi(p)
        if show:
            s._invoke(ref, 'OpenFrontPanel', True)
            s.prop_set('VI', 'BDWin.Open', ref, True)
        return {'path': str(p), 'diagram': s.diagram(p), 'panel': s.prop_get('VI', 'Panel', s.vi(p))}
    return await native(open_)


@mcp.tool()
async def add_object(owner: int, style: str | int | None = None, location: list[int] | None = None,
                     bounds: list[int] | None = None, path: str | None = None,
                     object_class: str = 'Generic') -> int:
    """Place a native node/control. Array elements use the array ref as owner.

    Numeric representation is set through Numeric.Rep (U8=8, U16=7).
    Use invoke/set_property to configure modes/content before inspecting terminals.
    """
    return await native(lambda s: s.new_object(owner, style, tuple(location) if location else None,
                                             tuple(bounds) if bounds else None, path, object_class))


@mcp.tool()
async def find_objects(path: str, object_class: str, panel: bool = False) -> list[int]:
    """Rediscover objects, especially after mode changes, replacement or revert."""
    return await native(lambda s: s.find(path, object_class, target=0 if panel else 1))


@mcp.tool()
async def inspect_node(reference: int) -> dict:
    """Read current node class and terminals. Rediscover after changing configuration."""
    def inspect(s):
        return {'class': s.prop_get('GObject', 'Class Name', reference),
                'terminals': [{'reference': t, 'name': s.prop_get('Terminal', 'Name', t),
                               'source': bool(s.prop_get('Terminal', 'Is Source?', t))}
                              for t in s.prop_get('Node', 'Terminals[]', reference)]}
    return await native(inspect)


@mcp.tool()
async def get_property(object_class: str, member: str, reference: int) -> dict:
    """Read a native property, returning {value: ...}; arrays retain their shape."""
    return {'value': await native(lambda s: s.prop_get(object_class, member, reference))}


@mcp.tool()
async def set_property(object_class: str, member: str, reference: int, value) -> dict:
    """Edit content/mode. Formula.FormExpr edits code; Control.Indicator changes direction.

    JSON clusters use {type: cluster, value: [...]}; plain lists remain arrays.
    Editing requires a stopped, unreserved VI; native errors are returned, not suppressed.
    """
    await native(lambda s: s.prop_set(object_class, member, reference, decode(value)))
    return {'updated': True}


@mcp.tool()
async def invoke(object_class: str, member: str, reference: int, inputs: dict | None = None) -> dict:
    """Invoke a native editing method, including frame/terminal/mode configuration.

    Examples: Formula.Add Input (Position scalar, Name); GrowableFunction.Resize.AddChunk;
    Property.Set Properties[] takes ID Strings[] (canonical unique IDs). Change
    PropertyItem.IsWrite separately, then rediscover the property's terminal.
    Optional inputs not supplied remain unwired. Reinspect terminals after changes.
    """
    return await native(lambda s: s.method(object_class, member, reference,
                                         **{k: decode(v) for k, v in (inputs or {}).items()}))


@mcp.tool()
async def wire(source: int, destination: int) -> dict:
    """Connect current terminal references. Source must be an output terminal."""
    return await native(lambda s: s.method('Terminal', 'Connect Wire', destination, **{'Wire Source': source}))


@mcp.tool()
async def connector(path: str, pattern: int, assignments: dict[str, int]) -> dict:
    """Set connector pattern and assign control refs to terminal indices (JSON keys)."""
    def configure(s):
        steps = [('property', 'VI', 'ConPane.Ref', False)]
        ref = s.vi(path)
        s.chain(steps + [('property', 'ConnectorPane', 'Pattern', True)], ref, Pattern=pattern)
        for index, control in assignments.items():
            s.chain(steps + [('method', 'ConnectorPane', 'AssignCtrlToTerm', False)], ref,
                    Control=control, **{'Terminal Index': int(index)})
        return {'assigned': True}
    return await native(configure)


@mcp.tool()
async def save_vi(path: str) -> dict:
    """Save current edits to the same VI path."""
    await native(lambda s: s.save(path))
    return {'saved': str(Path(path).resolve())}


@mcp.tool()
async def run_vi(path: str, inputs: dict, outputs: list[str]) -> dict:
    """Execute synchronously with named panel values and return selected outputs.

    U8[] input: {type: u8_array, value: [49,50,...]}. All calls are serialized.
    """
    return await native(lambda s: s.run(path, {k: decode(v) for k, v in inputs.items()}, outputs))


@mcp.tool()
async def select_polymorphic(reference: int, instance_index: int) -> dict:
    """Select a polymorphic instance and return fresh terminals; disables automatic selection.

    Existing wires/controls may need rebuilding when the selected signature changes.
    Installed instance order can be read through PolymorphicVI.InstanceInfo.
    """
    def select(s):
        s.prop_set('PolymorphicSubVI', 'SelectedType', reference, instance_index)
        return {'selected': s.prop_get('PolymorphicSubVI', 'SelectedType', reference),
                'terminals': s._terminals(reference)}
    return await native(select)


@mcp.tool()
async def configure_express_parameters(instance_path: str, configuration_path: str,
                                       mappings: dict[str, str], values: dict) -> dict:
    """Persist mapped Express property-page parameters using NI's read/write helpers.

    mappings maps instance panel labels to config-dialog panel labels. Values use
    config labels. This edits the supplied instance, never the installed template.
    It does NOT regenerate Express implementations, change signal kind/terminal
    structure, invoke a configuration GUI, or promise arbitrary Express support.
    Simulate Signal Amplitude persistence is runtime-verified.
    """
    def configure(s):
        from lvscript import LV, NO_ERR, _check
        instance = Path(instance_path).resolve()
        if instance.is_relative_to(LV.resolve()):
            raise ValueError('Copy the Express instance outside the NI installation before editing')
        if not set(values).issubset(set(mappings.values())):
            raise ValueError('Every configured value must have an explicit label mapping')
        config = s.vi(configuration_path)
        shared = LV / 'vi.lib' / 'express' / 'express shared'
        out = s.call(s.vi(shared / 'ex_Read Properties.vi'), {
            'Ignore Errors': False, 'Control Data': cluster_array([(0, a, b) for a, b in mappings.items()]),
            'viref Configuration Dialog': config, 'viref Instance': s.vi(instance),
            'Output Cluster': ([], [], [], 0, [], 0), 'viref Configuration Dialog out': 0})
        for name, value in values.items():
            s._invoke(config, 'SetControlValue', name, decode(value))
        props = list(out['Output Cluster'])
        props[2] = cluster_array(props[2])
        result = s.call(s.vi(shared / 'ex_Write Properties.vi'), {
            'Output Cluster': tuple(props), 'error in': NO_ERR, 'error out': NO_ERR, 'Title': ''})
        _check(result['error out'], 'Express property-page persistence')
        s.save(instance)
        return {'saved': str(instance), 'values': {
            a: s._invoke(s.vi(instance), 'GetControlValue', a) for a in mappings}}
    return await native(configure)


@mcp.tool()
async def render_vi(path: str, surface: str = 'diagram') -> Image:
    """Return an actual exported PNG (diagram or panel), not just its filename."""
    if surface not in ('diagram', 'panel'):
        raise ValueError('surface must be diagram or panel')
    images = await native(lambda s: s.render(path, ROOT / 'work' / 'mcp_render' / Path(path).stem))
    if surface not in images:
        raise ValueError(f'No {surface} image exported')
    return Image(path=images[surface], format='png')


if __name__ == '__main__':
    mcp.run(transport='stdio')
