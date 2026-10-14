"""Negative paths and edge cases over real stdio MCP. Every failing call must return a
readable diagnostic and leave the STA session usable. Only temporary VIs are touched."""
import asyncio
import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
from lvscript import LV  # noqa: E402
from mcp_client import connect  # noqa: E402


async def main():
    root = Path(tempfile.mkdtemp(prefix='labview-mcp-boundary-'))
    passed, failed = [], []
    async with connect() as lv:
        vi = await lv.call('new_vi', path=str(root / 'Shapes.vi'))
        scalar = await lv.add(vi['panel'], 'Numeric Control (modern)')

        async def expect_error(name, **args):
            assert (await lv.error(name, **args)).strip(), 'error without diagnostic'
            assert await lv.get('GObject', 'Class Name', scalar)   # session still alive

        async def label(ref, name):
            await lv.set('Text', 'Text', await lv.get('Control', 'Label', ref), name)

        async def stale_is_source(ref):
            """An outdated terminal ref may error or still answer; either way it must not lie."""
            try:
                assert await lv.get('Terminal', 'Is Source?', ref) is True
            except RuntimeError:
                pass

        async def invalid_ref():
            await expect_error('get_property', object_class='GObject', member='Class Name', reference=0)

        async def unknown_style():
            await expect_error('add_object', owner=vi['diagram'], style='Boundary nonexistent style')

        async def unknown_member():
            await expect_error('get_property', object_class='GObject', member='BoundaryNonexistentMember', reference=scalar)

        async def overwrite():
            await lv.call('save_vi', path=vi['path'])
            digest = hashlib.sha256(Path(vi['path']).read_bytes()).hexdigest()
            await expect_error('new_vi', path=vi['path'])
            assert hashlib.sha256(Path(vi['path']).read_bytes()).hexdigest() == digest

        async def shapes():
            await lv.set('Control', 'Value', scalar, 12.5)
            assert await lv.get('Control', 'Value', scalar) == 12.5
            await lv.set('GObject', 'Position', scalar, {'type': 'cluster', 'value': [40, 60]})
            assert await lv.get('GObject', 'Position', scalar) == [40, 60]
            array = await lv.add(vi['panel'], 'Array (modern)')
            await lv.set('Numeric', 'Rep', await lv.add(array, 'Numeric Control (modern)'), 8)
            for values in ([], [7], [1, 2, 255]):
                await lv.set('Control', 'Value', array, {'type': 'u8_array', 'value': values})
                assert await lv.get('Control', 'Value', array) == values
            cluster = await lv.add(vi['panel'], 'Cluster (modern)')
            await lv.add(cluster, 'Numeric Control (modern)')
            await lv.add(cluster, 'String Control (modern)')
            await lv.set('Control', 'Value', cluster, {'type': 'cluster', 'value': [3.5, 'boundary']})
            assert await lv.get('Control', 'Value', cluster) == [3.5, 'boundary']

        async def parallel():
            names = await asyncio.gather(*(lv.get('GObject', 'Class Name', scalar) for _ in range(12)))
            assert len(set(names)) == 1
            refs = await asyncio.gather(*(lv.add(vi['diagram'], 'Add', [i * 60, 300]) for i in range(6)))
            assert len(set(refs)) == 6
            nodes = await asyncio.gather(*(lv.call('inspect_node', reference=r) for r in refs))
            assert all(n['class'] == 'Function' and len(n['terminals']) == 3 for n in nodes)

        async def nested():
            target = await lv.call('new_vi', path=str(root / 'Nested.vi'))
            outer = await lv.add(target['diagram'], 'For Loop', [200, 100], [600, 400])
            inner = await lv.add(await lv.get('Loop', 'Diagram', outer), 'For Loop', [300, 180], [300, 200])
            for loop, name in ((outer, 'outer_count'), (inner, 'inner_count')):
                count = (await lv.terminals(loop))['N']
                await label((await lv.invoke('Terminal', 'Create Control', count))['Create Control'], name)
            term = await lv.formula(await lv.get('Loop', 'Diagram', inner), 'y = x + 1;', ['x'], ['y'], [320, 200], [120, 80])
            _, x = await lv.control(target, 'x', [0, 200])
            await lv.call('wire', source=x, destination=term['x'])
            wires = await lv.call('find_objects', path=target['path'], object_class='Wire')
            assert wires and not any([await lv.get('Wire', 'Is Broken?', w) for w in wires])
            assert len(await lv.call('find_objects', path=target['path'], object_class='Tunnel')) >= 2
            await lv.call('save_vi', path=target['path'])
            assert await lv.call('run_vi', path=target['path'], inputs={'outer_count': 2, 'inner_count': 3, 'x': 7}, outputs=[]) == {}

        async def modes():
            target = await lv.call('new_vi', path=str(root / 'Modes.vi'))
            node = await lv.call('add_object', owner=target['diagram'],
                                 path=str(LV / 'vi.lib/Analysis/baseanly.llb/Mean.vi'))   # in 2018 and 2021
            old = await lv.call('select_polymorphic', reference=node, instance_index=1)
            fresh = await lv.call('select_polymorphic', reference=node, instance_index=0)
            await stale_is_source(old['terminals']['mean'])
            refs = []
            for member, terminal, name in [('Create Control', 'X', 'x'), ('Create Indicator', 'mean', 'mean')]:
                refs.append((await lv.invoke('Terminal', member, fresh['terminals'][terminal]))[member])
                await label(refs[-1], name)
            output = refs[1]
            previous = await lv.get('Control', 'Terminal', output)
            await lv.set('Control', 'Indicator', output, False)
            assert await lv.get('Terminal', 'Is Source?', await lv.get('Control', 'Terminal', output)) is True
            await stale_is_source(previous)
            await lv.set('Control', 'Indicator', output, True)
            await lv.call('save_vi', path=target['path'])
            result = await lv.call('run_vi', path=target['path'],
                                   inputs={'x': [1.0, 2.0, 3.0, 6.0]}, outputs=['mean'])
            assert result == {'mean': 3.0}, result

        async def broken():
            target = await lv.call('new_vi', path=str(root / 'Broken.vi'))
            formula = await lv.add(target['diagram'], 'Formula Node')
            await lv.set('Formula', 'FormExpr', formula, 'this is not valid formula syntax;')
            await lv.call('save_vi', path=target['path'])
            await expect_error('run_vi', path=target['path'], inputs={}, outputs=[])
            await lv.set('Formula', 'FormExpr', formula, '')
            await lv.call('save_vi', path=target['path'])
            assert await lv.call('run_vi', path=target['path'], inputs={}, outputs=[]) == {}

        for case in (invalid_ref, unknown_style, unknown_member, overwrite, shapes,
                     parallel, nested, modes, broken):
            try:
                await case()
                passed.append(case.__name__)
            except Exception as exc:  # report every case, then fail at the end
                failed.append({'case': case.__name__, 'error': repr(exc)})
            print(case.__name__, 'ok' if case.__name__ in passed else 'FAILED', flush=True)
    print(json.dumps({'scratch': str(root), 'passed': len(passed), 'failed': failed}, indent=2))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
