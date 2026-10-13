"""PID closed loop on a second-order plant, built from an empty VI through MCP.

For Loop with four shift registers (position, velocity, integral, previous error),
a Formula Node for controller + plant, and a waveform graph of setpoint vs response.
The VI is run and its output checked against the same simulation in Python.

    python examples/pid_loop.py   ->  examples/output/pid_loop/
"""
import asyncio
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
from mcp_client import connect  # noqa: E402

OUT = Path(__file__).resolve().parent / 'output' / 'pid_loop'
STEP = """float64 e, d, u;
e = sp - y;
ie2 = ie + e * dt;
d = (e - ep) / dt;
u = kp * e + ki * ie2 + kd * d;
if (u > 20) u = 20;
if (u < -20) u = -20;
v2 = v + dt * (16 * (u - y) - 2.4 * v);
y2 = y + dt * v2;
e2 = e;
r = sp;"""
DEFAULTS = {'Setpoint': 1.0, 'Kp': 3.0, 'Ki': 4.0, 'Kd': 0.2, 'dt (s)': 0.01, 'Steps': 340}


def simulate(sp, kp, ki, kd, dt, steps):
    """Same discrete step as the Formula Node, for verification."""
    y = v = ie = 0.0
    ep = sp
    ys = []
    for _ in range(int(steps)):
        e = sp - y
        ie += e * dt
        u = max(-20.0, min(20.0, kp * e + ki * ie + kd * (e - ep) / dt))
        v += dt * (16 * (u - y) - 2.4 * v)
        y += dt * v
        ep = e
        ys.append(y)
    return ys


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / 'PID_Loop.vi'
    if path.exists():
        path.unlink()
    async with connect() as lv:
        vi = await lv.call('new_vi', path=str(path))
        c = {}
        for j, (name, value) in enumerate(DEFAULTS.items()):
            c[name] = (await lv.control(vi, name, [20, 20 + j * 60], default=value))[1]
        _, graph = await lv.control(vi, 'Step Response', [200, 20], 'Waveform Graph (modern)',
                                    indicator=True, bounds=[520, 330])
        _, peak = await lv.control(vi, 'Peak', [200, 370], indicator=True)
        _, overshoot = await lv.control(vi, 'Overshoot (%)', [340, 370], indicator=True)
        _, final = await lv.control(vi, 'Final Value', [480, 370], indicator=True)

        loop = await lv.add(vi['diagram'], 'For Loop', [250, 60], [620, 380])
        body = await lv.get('Loop', 'Diagram', loop)
        regs = {}
        for j, name in enumerate(['y', 'v', 'ie', 'ep']):
            regs[name] = await lv.terminals((await lv.call('add_shift_register', loop=loop, y=60 + j * 60))['reference'])
        step = await lv.formula(body, STEP, ['sp', 'kp', 'ki', 'kd', 'dt', 'y', 'v', 'ie', 'ep'],
                                ['y2', 'v2', 'ie2', 'e2', 'r'], [360, 100], [330, 300])
        wire = lambda a, b: lv.call('wire', source=a, destination=b)  # noqa: E731
        loop_t = await lv.terminals(loop)
        await wire(c['Steps'], loop_t['N'])
        for control, term in [('Setpoint', 'sp'), ('Kp', 'kp'), ('Ki', 'ki'), ('Kd', 'kd'), ('dt (s)', 'dt')]:
            await wire(c[control], step[term])
        for name, out in [('y', 'y2'), ('v', 'v2'), ('ie', 'ie2'), ('ep', 'e2')]:
            await wire(regs[name]['previous'], step[name])
            await wire(step[out], regs[name]['next'])
        zero = await lv.add(vi['diagram'], 'Numeric Constant', [120, 300])
        zero_out = next(iter((await lv.terminals(zero)).values()))
        for name in ('y', 'v', 'ie'):
            await wire(zero_out, regs[name]['initial'])
        await wire(c['Setpoint'], regs['ep']['initial'])   # no derivative kick at step 0

        # Response and setpoint leave the loop through auto-indexed tunnels, one plot each.
        plots = await lv.add(vi['diagram'], 'Build Array', [950, 120])
        await lv.invoke('GrowableFunction', 'Resize.AddChunk', plots, **{'Chunks To Add': 1})
        ins = [t['reference'] for t in (await lv.call('inspect_node', reference=plots))['terminals'] if not t['source']]
        out = next(t['reference'] for t in (await lv.call('inspect_node', reference=plots))['terminals'] if t['source'])
        await wire(step['r'], ins[0])
        await wire(step['y2'], ins[1])
        await wire(out, graph)
        await wire(regs['y']['final'], final)

        extrema = await lv.add(vi['diagram'], 'Array Max & Min', [950, 300])
        mm = {k.casefold(): v for k, v in (await lv.terminals(extrema)).items()}
        # Reuse the y2 output tunnel: the source end of the wire feeding the graph's plot 1.
        feed = await lv.get('Wire', 'Terminals[]', await lv.get('Terminal', 'Connected Wire', ins[1]))
        y_array = [t for t in feed if await lv.get('Terminal', 'Is Source?', t)][0]
        await wire(y_array, mm['array'])
        await wire(mm['max value'], peak)
        os_ = await lv.formula(vi['diagram'], 'os = (pk - sp) / sp * 100;', ['pk', 'sp'], ['os'], [1100, 300], [200, 60])
        await wire(mm['max value'], os_['pk'])
        await wire(c['Setpoint'], os_['sp'])
        await wire(os_['os'], overshoot)

        await lv.call('clean_up_diagram', path=str(path))
        await lv.call('save_vi', path=str(path))
        result = await lv.call('run_vi', path=str(path), inputs=DEFAULTS,
                               outputs=['Peak', 'Overshoot (%)', 'Final Value', 'Step Response'])
        expected = simulate(*DEFAULTS.values())
        assert math.isclose(result['Final Value'], expected[-1], rel_tol=1e-9), (result['Final Value'], expected[-1])
        assert math.isclose(result['Peak'], max(expected), rel_tol=1e-9), (result['Peak'], max(expected))
        assert [round(x, 9) for x in result['Step Response'][1]] == [round(x, 9) for x in expected]
        await lv.call('save_vi', path=str(path))
        (OUT / 'diagram.png').write_bytes(await lv.png(path, 'diagram'))
        (OUT / 'panel.png').write_bytes(await lv.png(path, 'panel'))
        wires = await lv.call('find_objects', path=str(path), object_class='Wire')
        report = {'vi': str(path), 'wires': len(wires), 'shift_registers': 4,
                  'peak': result['Peak'], 'overshoot_percent': result['Overshoot (%)'],
                  'final_value': result['Final Value'], 'matches_python_simulation': True}
    (OUT / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
