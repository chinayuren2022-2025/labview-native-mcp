"""Build and execute a two-stage signal-processing VI via real stdio MCP."""
import asyncio
import base64
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def main():
    root = Path(__file__).resolve().parent.parent / 'work' / ('signal_demo_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
    root.mkdir(parents=True)
    report = {'directory': str(root), 'cases': []}
    server = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).with_name('labview_mcp.py'))])
    async with stdio_client(server) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            await client.initialize()
            async def call(name, **args):
                r = await client.call_tool(name, args)
                if r.isError:
                    raise RuntimeError(f'{name}: {r.content}')
                v = r.structuredContent
                if v is None:
                    v = json.loads(next(c.text for c in r.content if c.type == 'text'))
                if isinstance(v, dict) and set(v) == {'result'}:
                    v = v['result']
                return v['value'] if name == 'get_property' else v
            async def prop(c, m, r):
                return await call('get_property', object_class=c, member=m, reference=r)
            async def setp(c, m, r, v):
                return await call('set_property', object_class=c, member=m, reference=r, value=v)
            async def wire(a, b):
                return await call('wire', source=a, destination=b)
            async def label(c, name):
                await setp('Text', 'Text', await prop('Control', 'Label', c), name)
            vi = await call('new_vi', path=str(root / ('Signal_Processing_Demo_' + datetime.now().strftime('%H%M%S') + '.vi')))
            report['vi'] = vi['path']
            async def control(name, xy, indicator=False, array=False, default=None):
                c = await call('add_object', owner=vi['panel'], style='Array (modern)' if array else 'Numeric Control (modern)', location=xy)
                if array:
                    await call('add_object', owner=c, style='Numeric Control (modern)')
                await label(c, name)
                await setp('Control', 'Indicator', c, indicator)
                if default is not None:
                    await setp('Control', 'Value', c, default)
                    await setp('Control', 'DefVal', c, default)
                return await prop('Control', 'Terminal', c)
            controls = {}
            for j, (name, default) in enumerate([('Samples', 64), ('Amplitude', 2.0), ('Cycles', 3.0), ('Gain', 1.5), ('Offset', 0.25), ('Limit', 1.0)]):
                controls[name] = await control(name, [30, 40 + j * 65], default=default)
            raw = await control('Raw Signal', [300, 40], True, True)
            processed = await control('Processed Signal', [300, 200], True, True)
            stats = {name: await control(name, [600, 40+j*70], True) for j, name in enumerate(['Mean', 'Minimum', 'Maximum', 'Output Count'])}
            async def node(owner, query, xy, bounds=None):
                styles = await call('search_styles', query=query)
                exact = next((s for s in styles['matches'] if s['name'].casefold() == query.casefold()), None)
                if exact is None:
                    raise RuntimeError(f'No exact style {query}: {styles}')
                return await call('add_object', owner=owner, style=exact['id'], location=xy, bounds=bounds)
            async def terms(n):
                return (await call('inspect_node', reference=n))['terminals']
            async def formula(owner, code, inputs, outputs, xy, bounds):
                n = await node(owner, 'Formula Node', xy, bounds)
                for member, names in [('Add Input', inputs), ('Add Output', outputs)]:
                    for j, name in enumerate(names):
                        await call('invoke', object_class='Formula', member=member, reference=n, inputs={'Position': 20+j*25, 'Name': name})
                await setp('Formula', 'FormExpr', n, code)
                return {t['name']: t['reference'] for t in await terms(n)}
            loop1 = await node(vi['diagram'], 'For Loop', [200, 100], [420, 330])
            loop2 = await node(vi['diagram'], 'For Loop', [700, 100], [600, 330])
            roles1 = {t['role']: t['reference'] for t in await terms(loop1)}
            await wire(controls['Samples'], roles1['count'])
            d1, d2 = await prop('Loop', 'Diagram', loop1), await prop('Loop', 'Diagram', loop2)
            generate = await formula(d1, 'y = a * sin(6.283185307179586 * cycles * i / n);', ['a', 'cycles', 'i', 'n'], ['y'], [300, 160], [420, 190])
            for name, term in [('Amplitude','a'), ('Cycles','cycles'), ('Samples','n')]:
                await wire(controls[name], generate[term])
            await wire(roles1['iteration'], generate['i'])
            await wire(generate['y'], raw)
            async def primitive(owner, style, xy):
                n = await node(owner, style, xy)
                ts = await terms(n)
                print(style, ts, flush=True)
                return [t['reference'] for t in ts if not t['source']], [t['reference'] for t in ts if t['source']]
            mul_in, mul_out = await primitive(d2, 'Multiply', [800, 180])
            add_in, add_out = await primitive(d2, 'Add', [900, 180])
            # Raw array enters the second loop through an auto-indexed input tunnel.
            await wire(raw, mul_in[0])
            await wire(controls['Gain'], mul_in[1])
            await wire(mul_out[0], add_in[0])
            await wire(controls['Offset'], add_in[1])
            clip = await formula(d2, 'float64 bound;\nbound = abs(limit);\nif (x > bound) y = bound;\nelse if (x < -bound) y = -bound;\nelse y = x;', ['x', 'limit'], ['y'], [1000, 150], [250, 200])
            await wire(add_out[0], clip['x'])
            await wire(controls['Limit'], clip['limit'])
            await wire(clip['y'], processed)
            sum_in, sum_out = await primitive(vi['diagram'], 'Add Array Elements', [1400, 100])
            size_in, size_out = await primitive(vi['diagram'], 'Array Size', [1400, 230])
            div_in, div_out = await primitive(vi['diagram'], 'Divide', [1500, 150])
            mm_in, mm_out = await primitive(vi['diagram'], 'Array Max & Min', [1400, 340])
            await wire(processed, sum_in[0])
            await wire(processed, size_in[0])
            await wire(processed, mm_in[0])
            await wire(sum_out[0], div_in[1])
            await wire(size_out[0], div_in[0])
            await wire(size_out[0], stats['Output Count'])
            await wire(div_out[0], stats['Mean'])
            # Resolve extrema by native terminal names instead of positional assumptions.
            all_mm = await call('find_objects', path=vi['path'], object_class='Function')
            extrema = None
            for n in all_mm:
                if await prop('Node', 'Style', n) == 'Array Max & Min':
                    extrema = n
                    break
            mm = {t['name'].casefold(): t['reference'] for t in await terms(extrema)}
            print('extrema', mm, flush=True)
            await wire(mm['max value'], stats['Maximum'])
            await wire(mm['min value'], stats['Minimum'])
            async def render(surface, filename):
                r = await client.call_tool('render_vi', {'path': vi['path'], 'surface': surface})
                if r.isError:
                    raise RuntimeError(r.content)
                (root / filename).write_bytes(base64.b64decode(next(c.data for c in r.content if c.type == 'image')))
            await render('diagram', 'diagram_before.png')
            await call('clean_up_diagram', path=vi['path'])
            await call('save_vi', path=vi['path'])
            await render('diagram', 'diagram_after.png')
            await render('panel', 'panel.png')
            for values in [dict(Samples=64, Amplitude=2., Cycles=3., Gain=1.5, Offset=.25, Limit=1.), dict(Samples=17, Amplitude=.5, Cycles=1., Gain=-2., Offset=-.1, Limit=.4), dict(Samples=1, Amplitude=3., Cycles=2., Gain=2., Offset=.75, Limit=-.5)]:
                expected_raw = [values['Amplitude']*math.sin(2*math.pi*values['Cycles']*i/values['Samples']) for i in range(values['Samples'])]
                bound = abs(values['Limit'])
                expected = [max(-bound, min(bound, x*values['Gain']+values['Offset'])) for x in expected_raw]
                actual = await call('run_vi', path=vi['path'], inputs=values, outputs=['Raw Signal','Processed Signal',*stats])
                for key, xs in [('Raw Signal',expected_raw), ('Processed Signal',expected)]:
                    assert len(actual[key]) == len(xs), (key, actual)
                    assert all(math.isclose(a,b,rel_tol=1e-10,abs_tol=1e-10) for a,b in zip(actual[key],xs)), (key,actual)
                for key, target in [('Mean',sum(expected)/len(expected)),('Minimum',min(expected)),('Maximum',max(expected)),('Output Count',len(expected))]:
                    assert math.isclose(actual[key],target,rel_tol=1e-10,abs_tol=1e-10), (key,actual,target)
                report['cases'].append({'inputs': values, 'statistics': {k:actual[k] for k in stats}, 'passed': True})
            wires = await call('find_objects', path=vi['path'], object_class='Wire')
            report['wires'] = len(wires)
            report['broken_wires'] = sum([bool(await prop('Wire','Is Broken?',w)) for w in wires])
            assert report['broken_wires'] == 0
            report['for_loops'] = len(await call('find_objects', path=vi['path'], object_class='ForLoop'))
            # Leave the actual surface displaying a representative 64-sample result.
            await call('run_vi', path=vi['path'], inputs=report['cases'][0]['inputs'], outputs=['Mean'])
            await call('save_vi', path=vi['path'])
            await render('panel', 'panel.png')
            await call('open_vi', path=vi['path'], show=True)
    (root/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    asyncio.run(main())
