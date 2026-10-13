"""Workflow checks over real stdio MCP: style discovery, diagnostics, wiring with
reversed endpoints, clean-up preserving topology, loop / shift-register / constant
terminal roles, and real execution. Only temporary VIs are touched."""
import asyncio
import hashlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
from mcp_client import connect  # noqa: E402


async def main():
    root = Path(tempfile.mkdtemp(prefix='labview-mcp-usability-'))
    async with connect() as lv:
        def check(name):
            print('ok', name, flush=True)

        styles, offset = [], 0
        while True:
            page = await lv.call('search_styles', limit=100, offset=offset)
            styles += page['matches']
            offset += len(page['matches'])
            if offset >= page['total']:
                break
        assert len({s['name'] for s in styles}) == page['total']
        assert any(s['name'] == 'While Loop #1' for s in (await lv.call('search_styles', query='whlie loop'))['matches'])
        check(f'{len(styles)} native styles, paged and fuzzy-searchable')

        vi = await lv.call('new_vi', path=str(root / 'Conversion.vi'))
        assert 'While Loop #1' in await lv.error('add_object', owner=vi['diagram'], style='While Loop')
        digest = hashlib.sha256(Path(vi['path']).read_bytes()).hexdigest()
        assert 'already exists' in await lv.error('new_vi', path=vi['path'])
        assert hashlib.sha256(Path(vi['path']).read_bytes()).hexdigest() == digest
        assert 'HRESULT' in await lv.error('open_vi', path=str(root / 'Missing.vi'))
        assert 'Class Name' in await lv.error('get_property', object_class='GObject', member='Class Name', reference=0)
        hint = await lv.error('invoke', object_class='Loop', member='Add Shift Register', reference=vi['diagram'],
                              inputs={'Y Position': 0})
        assert 'Y Positon' in hint, hint      # LabVIEW's own spelling is suggested
        check('style candidates, overwrite refusal, COM and parameter diagnostics')

        _, c = await lv.control(vi, 'Celsius', [0, 0])
        _, f = await lv.control(vi, 'Fahrenheit', [200, 0], indicator=True)
        term = await lv.formula(vi['diagram'], 'F = C * 1.8 + 32;', ['C'], ['F'], [100, 100], [180, 100])
        for t in (c, f):    # pile everything on top of the formula, then wire with reversed endpoints
            await lv.set('GObject', 'Position', t, {'type': 'cluster', 'value': [100, 100]})
        await lv.call('wire', source=term['C'], destination=c)
        await lv.call('wire', source=f, destination=term['F'])

        async def topology():
            wires = await lv.call('find_objects', path=vi['path'], object_class='Wire')
            return sorted([sorted(await lv.get('Wire', 'Terminals[]', w)) for w in wires])

        connected = await topology()
        assert sorted([c, term['C']]) in connected and sorted([f, term['F']]) in connected
        positions = [await lv.get('GObject', 'Position', t) for t in (c, f)]
        await lv.call('clean_up_diagram', path=vi['path'])
        assert await topology() == connected
        assert positions != [await lv.get('GObject', 'Position', t) for t in (c, f)]
        await lv.call('save_vi', path=vi['path'])
        for celsius, fahrenheit in [(100, 212), (0, 32)]:
            assert (await lv.call('run_vi', path=vi['path'], inputs={'Celsius': celsius}, outputs=['Fahrenheit']))['Fahrenheit'] == fahrenheit
        check('reversed wiring, clean-up keeps topology, 100 C -> 212 F')

        for style, cls in [('While Loop #1', 'WhileLoop'), ('For Loop', 'ForLoop')]:
            loopvi = await lv.call('new_vi', path=str(root / f'{cls}.vi'))
            loop = await lv.add(loopvi['diagram'], style, [150, 100], [350, 200])
            info = await lv.call('inspect_node', reference=loop)
            assert info['class'] == cls
            roles = {t['role']: t for t in info['terminals']}
            assert roles['iteration']['name'] == 'i' and roles['iteration']['reference'] == await lv.get('Loop', 'LoopCounter', loop)
            _, last = await lv.control(loopvi, 'LastIteration', [0, 0], indicator=True)
            await lv.call('wire', source=roles['iteration']['reference'], destination=last)
            for tunnel in await lv.call('find_objects', path=loopvi['path'], object_class='LoopTunnel'):
                await lv.set('LoopTunnel', 'IndexMode', tunnel, 0)
            if cls == 'WhileLoop':
                assert roles['condition']['reference'] == await lv.get(cls, 'LoopEndRef', loop)
                button = (await lv.call('search_styles', query='Push Button'))['matches'][0]['name']
                _, stop = await lv.control(loopvi, 'Stop', [0, 80], style=button)
                await lv.call('wire', source=stop, destination=roles['condition']['reference'])
                inputs, expected = {'Stop': True}, 0
            else:
                assert roles['count']['name'] == 'N' and 'condition' not in roles
                await lv.set(cls, 'HasCondTerm', loop, True)
                assert any(t['role'] == 'condition' for t in (await lv.call('inspect_node', reference=loop))['terminals'])
                await lv.set(cls, 'HasCondTerm', loop, False)
                _, count = await lv.control(loopvi, 'Count', [0, 80])
                await lv.call('wire', source=count, destination=roles['count']['reference'])
                inputs, expected = {'Count': 3}, 2
            await lv.call('save_vi', path=loopvi['path'])
            assert (await lv.call('run_vi', path=loopvi['path'], inputs=inputs, outputs=['LastIteration']))['LastIteration'] == expected
            check(f'{cls} terminal roles and execution')

        # Running sum 1..N through a shift register initialised from a constant.
        vi = await lv.call('new_vi', path=str(root / 'ShiftRegister.vi'))
        loop = await lv.add(vi['diagram'], 'For Loop', [200, 60], [300, 200])
        reg = await lv.call('add_shift_register', loop=loop)
        assert {t['role'] for t in reg['terminals']} == {'initial', 'previous', 'next', 'final'}
        reg = {t['role']: t['reference'] for t in reg['terminals']}
        zero = await lv.call('inspect_node', reference=await lv.add(vi['diagram'], 'Numeric Constant', [100, 120]))
        assert [t['role'] for t in zero['terminals']] == ['value']
        add = await lv.terminals(await lv.add(await lv.get('Loop', 'Diagram', loop), 'Add', [300, 120]))
        increment = await lv.terminals(await lv.add(await lv.get('Loop', 'Diagram', loop), 'Increment', [260, 160]))
        loop_t = await lv.terminals(loop)
        _, n = await lv.control(vi, 'N', [0, 0])
        _, total = await lv.control(vi, 'Sum', [200, 0], indicator=True)
        for a, b in [(zero['terminals'][0]['reference'], reg['initial']), (reg['previous'], add['x']),
                     (loop_t['i'], increment['x']), (increment['x+1'], add['y']), (add['x+y'], reg['next']),
                     (reg['final'], total), (n, loop_t['N'])]:
            await lv.call('wire', source=a, destination=b)
        await lv.call('clean_up_diagram', path=vi['path'])
        await lv.call('save_vi', path=vi['path'])
        assert (await lv.call('run_vi', path=vi['path'], inputs={'N': 10}, outputs=['Sum']))['Sum'] == 55
        check('shift register + constant roles, sum 1..10 = 55')
    print('USABILITY_OK', root)


if __name__ == '__main__':
    asyncio.run(main())
