"""Real stdio / LabVIEW regression for discovery, loops, layout and errors.

Only temporary VIs; no user VIs, serial ports or hardware. Prints report paths.
"""
import asyncio
import base64
import hashlib
import json
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def main():
    root = Path(tempfile.mkdtemp(prefix='labview-mcp-usability-'))
    report = {'scratch': str(root), 'checks': [], 'images': []}
    server = StdioServerParameters(command=sys.executable,
                                  args=[str(Path(__file__).with_name('labview_mcp.py'))])
    async with stdio_client(server) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            await client.initialize()

            async def call(name, **args):
                result = await client.call_tool(name, args)
                if result.isError:
                    raise RuntimeError(f'{name}: {result.content}')
                value = result.structuredContent
                if value is None:
                    value = json.loads(next(c.text for c in result.content if c.type == 'text'))
                if isinstance(value, dict) and set(value) == {'result'}:
                    value = value['result']
                return value['value'] if name == 'get_property' else value

            async def error(name, **args):
                result = await client.call_tool(name, args)
                assert result.isError, f'{name} unexpectedly succeeded'
                text = '\n'.join(c.text for c in result.content if c.type == 'text')
                report.setdefault('errors', []).append(text)
                return text

            async def prop(cls, member, ref):
                return await call('get_property', object_class=cls, member=member, reference=ref)

            async def setprop(cls, member, ref, value):
                return await call('set_property', object_class=cls, member=member, reference=ref, value=value)

            async def numeric(vi, name, indicator=False):
                ctl = await call('add_object', owner=vi['panel'], style='Numeric Control (modern)')
                await setprop('Text', 'Text', await prop('Control', 'Label', ctl), name)
                await setprop('Control', 'Indicator', ctl, indicator)
                return ctl, await prop('Control', 'Terminal', ctl)

            async def image(path, name):
                result = await client.call_tool('render_vi', {'path': path})
                assert not result.isError, result.content
                data = base64.b64decode(next(c.data for c in result.content if c.type == 'image'))
                assert data.startswith(b'\x89PNG\r\n\x1a\n')
                output = root / f'{name}.png'
                output.write_bytes(data)
                report['images'].append(str(output))
                return hashlib.sha256(data).hexdigest()

            styles = await call('search_styles', query='While Loop')
            while_style = next(x for x in styles['matches'] if x['name'] == 'While Loop #1')
            all_styles = []
            offset = 0
            while True:
                page = await call('search_styles', limit=100, offset=offset)
                all_styles.extend(page['matches'])
                offset += len(page['matches'])
                if offset >= page['total']:
                    break
            assert len({x['name'] for x in all_styles}) == page['total']
            assert while_style in all_styles
            report['native_styles'] = len(all_styles)
            assert any(x['name'] == 'While Loop #1' for x in
                       (await call('search_styles', query='whlie loop'))['matches'])
            report['checks'].append('native catalog pagination and fuzzy discovery')

            vi = await call('new_vi', path=str(root / 'Conversion.vi'))
            diagnostic = await error('add_object', owner=vi['diagram'], style='While Loop')
            assert 'While Loop #1' in diagnostic
            before_file = hashlib.sha256(Path(vi['path']).read_bytes()).hexdigest()
            diagnostic = await error('new_vi', path=vi['path'])
            assert 'already exists' in diagnostic and 'not overwritten' in diagnostic
            assert hashlib.sha256(Path(vi['path']).read_bytes()).hexdigest() == before_file
            diagnostic = await error('open_vi', path=str(root / 'Missing.vi'))
            assert 'HRESULT' in diagnostic and not diagnostic.startswith('(')
            diagnostic = await error('get_property', object_class='GObject', member='Class Name', reference=0)
            assert 'error' in diagnostic.lower() and 'Class Name' in diagnostic
            assert await prop('GObject', 'Class Name', vi['diagram'])
            report['checks'].append('candidate, overwrite and native error recovery')

            c, ct = await numeric(vi, 'Celsius')
            f, ft = await numeric(vi, 'Fahrenheit', True)
            formula = await call('add_object', owner=vi['diagram'], style='Formula Node',
                                 location=[100, 100], bounds=[180, 100])
            for member, name in [('Add Input', 'C'), ('Add Output', 'F')]:
                await call('invoke', object_class='Formula', member=member, reference=formula,
                           inputs={'Position': 0, 'Name': name})
            await setprop('Formula', 'FormExpr', formula, 'F = C * 1.8 + 32;')
            terms = {t['name']: t['reference'] for t in
                     (await call('inspect_node', reference=formula))['terminals']}
            # Deliberately overlap both controls with the formula, then reverse endpoints.
            for t in (ct, ft):
                await setprop('GObject', 'Position', t, {'type': 'cluster', 'value': [100, 100]})
            await call('wire', source=terms['C'], destination=ct)
            await call('wire', source=ft, destination=terms['F'])

            async def topology():
                wires = await call('find_objects', path=vi['path'], object_class='Wire')
                connections = []
                for w in wires:
                    connections.append(sorted(await prop('Wire', 'Terminals[]', w)))
                return sorted(connections)

            connected = await topology()
            assert sorted([ct, terms['C']]) in connected
            assert sorted([ft, terms['F']]) in connected
            assert sorted([ct, ft]) not in connected
            first_image = await image(vi['path'], 'before_cleanup')
            assert await topology() == connected
            assert (await call('run_vi', path=vi['path'], inputs={'Celsius': 100}, outputs=['Fahrenheit']))['Fahrenheit'] == 212
            positions = [await prop('GObject', 'Position', t) for t in (ct, ft, formula)]
            await call('clean_up_diagram', path=vi['path'])
            assert await topology() == connected
            assert positions != [await prop('GObject', 'Position', t) for t in (ct, ft, formula)]
            second_image = await image(vi['path'], 'after_cleanup')
            assert first_image != second_image
            await call('save_vi', path=vi['path'])
            await call('open_vi', path=vi['path'])
            assert (await call('run_vi', path=vi['path'], inputs={'Celsius': 0}, outputs=['Fahrenheit']))['Fahrenheit'] == 32
            report['checks'].append('reverse wiring, exact topology, conversion and explicit cleanup')

            for style, cls in [('While Loop #1', 'WhileLoop'), ('For Loop', 'ForLoop')]:
                loopvi = await call('new_vi', path=str(root / f'{cls}.vi'))
                loop = await call('add_object', owner=loopvi['diagram'], style=style,
                                  location=[150, 100], bounds=[350, 200])
                info = await call('inspect_node', reference=loop)
                assert info['class'] == cls
                roles = {t['role']: t for t in info['terminals']}
                assert roles['iteration']['name'] == 'i' and roles['iteration']['source']
                assert roles['iteration']['reference'] == await prop('Loop', 'LoopCounter', loop)
                _, output = await numeric(loopvi, 'LastIteration', True)
                await call('wire', source=roles['iteration']['reference'], destination=output)
                for tunnel in await call('find_objects', path=loopvi['path'], object_class='LoopTunnel'):
                    await setprop('LoopTunnel', 'IndexMode', tunnel, 0)
                if cls == 'WhileLoop':
                    assert roles['condition']['reference'] == await prop(cls, 'LoopEndRef', loop)
                    boolean_styles = await call('search_styles', query='Push Button')
                    stop = await call('add_object', owner=loopvi['panel'], style=boolean_styles['matches'][0]['name'])
                    await setprop('Text', 'Text', await prop('Control', 'Label', stop), 'Stop')
                    await call('wire', source=await prop('Control', 'Terminal', stop),
                               destination=roles['condition']['reference'])
                    inputs, expected = {'Stop': True}, 0
                else:
                    assert roles['count']['name'] == 'N' and not roles['count']['source']
                    assert 'condition' not in roles
                    count_tunnel = await prop(cls, 'LoopCount', loop)
                    assert roles['count']['reference'] == await prop('Tunnel', 'Outside Term', count_tunnel)
                    _, count = await numeric(loopvi, 'Count')
                    await call('wire', source=count, destination=roles['count']['reference'])
                    await setprop(cls, 'HasCondTerm', loop, True)
                    optional = await call('inspect_node', reference=loop)
                    assert any(t['role'] == 'condition' for t in optional['terminals'])
                    await setprop(cls, 'HasCondTerm', loop, False)
                    inputs, expected = {'Count': 3}, 2
                await call('save_vi', path=loopvi['path'])
                value = await call('run_vi', path=loopvi['path'], inputs=inputs, outputs=['LastIteration'])
                assert value['LastIteration'] == expected, value
                report['checks'].append(f'{cls} semantic terminals and real execution')
    (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
