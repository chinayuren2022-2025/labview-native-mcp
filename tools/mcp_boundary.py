"""Exercise finite native LabVIEW boundaries through the real stdio MCP client.

Only newly created temporary VIs are edited or run; no serial or hardware nodes.
Results describe these cases, not arbitrary node, datatype or STA support.
"""
import asyncio
import hashlib
import json
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def main():
    root = Path(tempfile.mkdtemp(prefix='labview-mcp-boundary-'))
    report = {'scratch': str(root), 'passed': [], 'failed': [], 'observations': [],
              'requests': 0, 'native_errors': 0}
    server = StdioServerParameters(command=sys.executable,
                                  args=[str(Path(__file__).with_name('labview_mcp.py'))])
    async with stdio_client(server) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            await client.initialize()

            async def raw(name, **args):
                report['requests'] += 1
                result = await client.call_tool(name, args)
                if result.isError:
                    report['native_errors'] += 1
                return result

            def unpack(result):
                obj = result.structuredContent
                if obj is None:
                    obj = json.loads(next(c.text for c in result.content if c.type == 'text'))
                return obj['result'] if isinstance(obj, dict) and set(obj) == {'result'} else obj

            async def call(name, **args):
                result = await raw(name, **args)
                if result.isError:
                    raise RuntimeError(f'{name}: {result.content}')
                value = unpack(result)
                return value['value'] if name == 'get_property' else value

            async def prop(cls, member, ref):
                return await call('get_property', object_class=cls, member=member, reference=ref)

            async def setprop(cls, member, ref, value):
                return await call('set_property', object_class=cls, member=member, reference=ref, value=value)

            async def expect_error(name, **args):
                result = await raw(name, **args)
                assert result.isError, f'{name} unexpectedly succeeded: {result.content}'
                text = '\n'.join(c.text for c in result.content if c.type == 'text')
                assert text.strip(), 'MCP error must include diagnostics'
                report['observations'].append({'error_tool': name, 'diagnostic': text})
                # Every negative request must leave the same STA service usable.
                assert await prop('GObject', 'Class Name', scalar)

            async def case(name, operation):
                try:
                    await operation()
                    report['passed'].append(name)
                except Exception as exc:
                    report['failed'].append({'category': name, 'error': str(exc)})
                print(json.dumps({'category': name, 'passed': name in report['passed']}), flush=True)

            vi = await call('new_vi', path=str(root / 'Shapes.vi'))
            scalar = await call('add_object', owner=vi['panel'], style='Numeric Control (modern)')

            async def invalid_ref():
                await expect_error('get_property', object_class='GObject', member='Class Name', reference=0)

            async def unknown_style():
                await expect_error('add_object', owner=vi['diagram'], style='Boundary nonexistent style')

            async def unknown_member():
                await expect_error('get_property', object_class='GObject', member='BoundaryNonexistentMember', reference=scalar)

            async def overwrite():
                await call('save_vi', path=vi['path'])
                before = hashlib.sha256(Path(vi['path']).read_bytes()).hexdigest()
                await expect_error('new_vi', path=vi['path'])
                assert hashlib.sha256(Path(vi['path']).read_bytes()).hexdigest() == before

            async def shapes():
                await setprop('Control', 'Value', scalar, 12.5)
                assert await prop('Control', 'Value', scalar) == 12.5
                position = {'type': 'cluster', 'value': [40, 60]}
                await setprop('GObject', 'Position', scalar, position)
                assert await prop('GObject', 'Position', scalar) == [40, 60]
                array = await call('add_object', owner=vi['panel'], style='Array (modern)')
                element = await call('add_object', owner=array, style='Numeric Control (modern)')
                await setprop('Numeric', 'Rep', element, 8)
                for values in ([], [7], [1, 2, 255]):
                    await setprop('Control', 'Value', array, {'type': 'u8_array', 'value': values})
                    actual = await prop('Control', 'Value', array)
                    assert actual == values, {'expected_array': values, 'actual': actual}
                cluster = await call('add_object', owner=vi['panel'], style='Cluster (modern)')
                await call('add_object', owner=cluster, style='Numeric Control (modern)')
                await call('add_object', owner=cluster, style='String Control (modern)')
                await setprop('Control', 'Value', cluster, {'type': 'cluster', 'value': [3.5, 'boundary']})
                assert await prop('Control', 'Value', cluster) == [3.5, 'boundary']

            async def parallel():
                values = await asyncio.gather(*(prop('GObject', 'Class Name', scalar) for _ in range(12)))
                assert len(values) == 12 and len(set(values)) == 1
                refs = await asyncio.gather(*(call('add_object', owner=vi['diagram'], style='Add',
                                                  location=[i * 60, 300]) for i in range(6)))
                assert len(set(refs)) == 6
                nodes = await asyncio.gather(*(call('inspect_node', reference=r) for r in refs))
                assert all(n['class'] == 'Function' and len(n['terminals']) == 3 for n in nodes)

            async def label(ref, name):
                await setprop('Text', 'Text', await prop('Control', 'Label', ref), name)

            async def nested():
                target = await call('new_vi', path=str(root / 'Nested.vi'))
                outer = await call('add_object', owner=target['diagram'], style='For Loop',
                                   location=[200, 100], bounds=[600, 400])
                outer_diagram = await prop('Loop', 'Diagram', outer)
                inner = await call('add_object', owner=outer_diagram, style='For Loop',
                                   location=[300, 180], bounds=[300, 200])
                inner_diagram = await prop('Loop', 'Diagram', inner)
                for loop, name in ((outer, 'outer_count'), (inner, 'inner_count')):
                    terminals = (await call('inspect_node', reference=loop))['terminals']
                    sinks = [t['reference'] for t in terminals if not t['source']]
                    assert len(sinks) == 1, terminals
                    n = sinks[0]
                    control = (await call('invoke', object_class='Terminal', member='Create Control', reference=n))['Create Control']
                    await label(control, name)
                formula = await call('add_object', owner=inner_diagram, style='Formula Node')
                await setprop('Formula', 'FormExpr', formula, 'y = x + 1;')
                x = (await call('invoke', object_class='Formula', member='Add Input', reference=formula,
                                inputs={'Position': 20, 'Name': 'x'}))['Add Input']
                await call('invoke', object_class='Formula', member='Add Output', reference=formula,
                           inputs={'Position': 40, 'Name': 'y'})
                control = await call('add_object', owner=target['panel'], style='Numeric Control (modern)')
                await label(control, 'x')
                await call('wire', source=await prop('Control', 'Terminal', control),
                           destination=await prop('FormulaParameter', 'Terminal', x))
                wires = await call('find_objects', path=target['path'], object_class='Wire')
                assert wires and not any([await prop('Wire', 'Is Broken?', w) for w in wires])
                tunnels = await call('find_objects', path=target['path'], object_class='Tunnel')
                assert len(tunnels) >= 2, tunnels
                await call('save_vi', path=target['path'])
                assert await call('run_vi', path=target['path'], inputs={'outer_count': 2, 'inner_count': 3, 'x': 7}, outputs=[]) == {}
                report['observations'].append({'crossdiagram_tunnels': len(tunnels), 'wire_segments': len(wires),
                                               'loop_iterations': [2, 3]})

            async def modes():
                from lvscript import LV
                target = await call('new_vi', path=str(root / 'Modes.vi'))
                node = await call('add_object', owner=target['diagram'],
                                  path=str(LV / 'vi.lib' / 'Bit Manipulation' / 'Little-Endian Bytes To Words Poly.vi'))
                old = await call('select_polymorphic', reference=node, instance_index=2)
                fresh = await call('select_polymorphic', reference=node, instance_index=4)
                assert old['selected'] == 2 and fresh['selected'] == 4
                stale = await raw('get_property', object_class='Terminal', member='Is Source?',
                                  reference=old['terminals']['words out'])
                if not stale.isError:
                    assert unpack(stale)['value'] is True
                report['observations'].append({'old_polymorphic_terminal_error': bool(stale.isError),
                                               'old_reference': old['terminals']['words out'],
                                               'fresh_reference': fresh['terminals']['words out']})
                controls = []
                for member, terminal, name in [('Create Control', 'bytes in', 'bytes'),
                                                ('Create Indicator', 'words out', 'words')]:
                    ref = (await call('invoke', object_class='Terminal', member=member,
                                      reference=fresh['terminals'][terminal]))[member]
                    await label(ref, name)
                    controls.append(ref)
                output = controls[1]
                previous_terminal = await prop('Control', 'Terminal', output)
                assert await prop('Terminal', 'Is Source?', previous_terminal) is False
                await setprop('Control', 'Indicator', output, False)
                current_terminal = await prop('Control', 'Terminal', output)
                assert await prop('Terminal', 'Is Source?', current_terminal) is True
                previous = await raw('get_property', object_class='Terminal', member='Is Source?', reference=previous_terminal)
                if not previous.isError:
                    assert unpack(previous)['value'] is True
                report['observations'].append({'old_direction_terminal_error': bool(previous.isError),
                                               'old_reference': previous_terminal, 'fresh_reference': current_terminal})
                await setprop('Control', 'Indicator', output, True)
                await call('save_vi', path=target['path'])
                result = await call('run_vi', path=target['path'],
                                    inputs={'bytes': {'type': 'u8_array', 'value': [1, 2, 3, 4]}}, outputs=['words'])
                assert result == {'words': [513, 1027]}, result

            async def broken():
                target = await call('new_vi', path=str(root / 'Broken.vi'))
                formula = await call('add_object', owner=target['diagram'], style='Formula Node')
                await setprop('Formula', 'FormExpr', formula, 'this is not valid formula syntax;')
                await call('save_vi', path=target['path'])
                await expect_error('run_vi', path=target['path'], inputs={}, outputs=[])
                await setprop('Formula', 'FormExpr', formula, '')
                await call('save_vi', path=target['path'])
                assert await call('run_vi', path=target['path'], inputs={}, outputs=[]) == {}

            for name, operation in [('invalid_ref_recovery', invalid_ref), ('unknown_style_recovery', unknown_style),
                                    ('unknown_member_recovery', unknown_member), ('overwrite_refusal', overwrite),
                                    ('scalar_array_cluster_shape', shapes), ('parallel_STA_requests', parallel),
                                    ('nested_crossdiagram_wire', nested), ('mode_changes_fresh_terminals', modes),
                                    ('broken_run_error_and_repaired_run', broken)]:
                await case(name, operation)
    report['passed_count'] = len(report['passed'])
    report['failed_count'] = len(report['failed'])
    (root / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2), flush=True)
    return 1 if report['failed'] else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
