"""Two-loop signal chain built from an empty VI through MCP.

Loop 1 generates a sine wave in a Formula Node; loop 2 applies gain/offset with
native Multiply/Add and clips it in a second Formula Node. Raw and processed
signals go to a waveform graph; mean / min / max are computed with array
primitives. Results are checked point by point against Python.

    python examples/signal_chain.py   ->  examples/output/signal_chain/
"""
import asyncio
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
from mcp_client import connect  # noqa: E402

OUT = Path(__file__).resolve().parent / 'output' / 'signal_chain'
GENERATE = 'y = a * sin(2 * pi * cycles * i / n);'
CLIP = """float64 b;
b = abs(limit);
if (x > b) y = b;
else if (x < -b) y = -b;
else y = x;"""
DEFAULTS = {'Samples': 200, 'Amplitude': 2.0, 'Cycles': 3.0, 'Gain': 1.5, 'Offset': 0.25, 'Limit': 2.0}


def expected(Samples, Amplitude, Cycles, Gain, Offset, Limit):
    raw = [Amplitude * math.sin(2 * math.pi * Cycles * i / Samples) for i in range(Samples)]
    b = abs(Limit)
    return raw, [max(-b, min(b, x * Gain + Offset)) for x in raw]


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / 'Signal_Chain.vi'
    if path.exists():
        path.unlink()
    async with connect() as lv:
        wire = lambda a, b: lv.call('wire', source=a, destination=b)  # noqa: E731
        vi = await lv.call('new_vi', path=str(path))
        c = {}
        for j, (name, value) in enumerate(DEFAULTS.items()):
            c[name] = (await lv.control(vi, name, [20, 20 + j * 60], default=value))[1]
        _, graph = await lv.control(vi, 'Raw vs Processed', [200, 20], 'Waveform Graph (modern)',
                                    indicator=True, bounds=[520, 330])
        stats = {}
        for j, name in enumerate(['Mean', 'Minimum', 'Maximum']):
            stats[name] = (await lv.control(vi, name, [200 + j * 140, 370], indicator=True))[1]

        gen_loop = await lv.add(vi['diagram'], 'For Loop', [200, 100], [380, 260])
        proc_loop = await lv.add(vi['diagram'], 'For Loop', [700, 100], [520, 260])
        gen_t = await lv.terminals(gen_loop)
        await wire(c['Samples'], gen_t['N'])
        gen = await lv.formula(await lv.get('Loop', 'Diagram', gen_loop), GENERATE,
                               ['a', 'cycles', 'i', 'n'], ['y'], [260, 150], [300, 80])
        for control, term in [('Amplitude', 'a'), ('Cycles', 'cycles'), ('Samples', 'n')]:
            await wire(c[control], gen[term])
        await wire(gen_t['i'], gen['i'])

        body = await lv.get('Loop', 'Diagram', proc_loop)
        mul = await lv.add(body, 'Multiply', [760, 180])
        add = await lv.add(body, 'Add', [840, 180])
        clip = await lv.formula(body, CLIP, ['x', 'limit'], ['y'], [920, 140], [240, 140])
        mul_t, add_t = await lv.terminals(mul), await lv.terminals(add)
        await wire(gen['y'], mul_t['x'])          # auto-indexed: array out of loop 1, element in loop 2
        await wire(c['Gain'], mul_t['y'])
        await wire(mul_t['x*y'], add_t['x'])
        await wire(c['Offset'], add_t['y'])
        await wire(add_t['x+y'], clip['x'])
        await wire(c['Limit'], clip['limit'])

        plots = await lv.add(vi['diagram'], 'Build Array', [1300, 120])
        await lv.invoke('GrowableFunction', 'Resize.AddChunk', plots, **{'Chunks To Add': 1})
        plot_t = (await lv.call('inspect_node', reference=plots))['terminals']
        plot_in = [t['reference'] for t in plot_t if not t['source']]
        await wire(gen['y'], plot_in[0])
        await wire(clip['y'], plot_in[1])
        await wire(next(t['reference'] for t in plot_t if t['source']), graph)

        # The processed array is the source end of the wire into plot 1.
        feed = await lv.get('Wire', 'Terminals[]', await lv.get('Terminal', 'Connected Wire', plot_in[1]))
        processed = [t for t in feed if await lv.get('Terminal', 'Is Source?', t)][0]
        total = await lv.terminals(await lv.add(vi['diagram'], 'Add Array Elements', [1300, 260]))
        size = await lv.terminals(await lv.add(vi['diagram'], 'Array Size', [1300, 320]))
        div = await lv.terminals(await lv.add(vi['diagram'], 'Divide', [1400, 280]))
        mm = {k.casefold(): v for k, v in (await lv.terminals(await lv.add(vi['diagram'], 'Array Max & Min', [1300, 400]))).items()}
        await wire(processed, total['numeric array'])
        await wire(processed, size['array'])
        await wire(processed, mm['array'])
        await wire(total['sum'], div['x'])
        await wire(size['size(s)'], div['y'])
        await wire(div['x/y'], stats['Mean'])
        await wire(mm['max value'], stats['Maximum'])
        await wire(mm['min value'], stats['Minimum'])

        await lv.call('clean_up_diagram', path=str(path))
        await lv.call('save_vi', path=str(path))
        cases = [DEFAULTS, dict(DEFAULTS, Samples=17, Gain=-2.0, Limit=0.4)]
        for case in reversed(cases):      # end on the default case so the panel shows it
            got = await lv.call('run_vi', path=str(path), inputs=case,
                                outputs=['Raw vs Processed', 'Mean', 'Minimum', 'Maximum'])
            raw, proc = expected(**case)
            for a, b in zip(got['Raw vs Processed'][0] + got['Raw vs Processed'][1], raw + proc):
                assert math.isclose(a, b, abs_tol=1e-9), (a, b)
            assert math.isclose(got['Mean'], sum(proc) / len(proc), abs_tol=1e-9)
            assert (got['Minimum'], got['Maximum']) == (min(proc), max(proc))
        await lv.call('save_vi', path=str(path))
        (OUT / 'diagram.png').write_bytes(await lv.png(path, 'diagram'))
        (OUT / 'panel.png').write_bytes(await lv.png(path, 'panel'))
        wires = await lv.call('find_objects', path=str(path), object_class='Wire')
        report = {'vi': str(path), 'wires': len(wires), 'cases_checked': len(cases),
                  'mean': got['Mean'], 'minimum': got['Minimum'], 'maximum': got['Maximum']}
    (OUT / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
