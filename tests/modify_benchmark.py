"""Real MCP 3 -> 4 channel editing benchmark; no DAQ/PID toolkit or hardware.

Run with the repository Python 3.12 and LABVIEW_VERSION=2021 or 2018. Each run
creates unique VIs/cache namespaces; no existing worker directory is removed.
Reports and complete request/response traces go to --output (default: work/...).
Build/setup, modification, verification and negative-path calls are separate.
"""
import argparse
import asyncio
import hashlib
import json
import math
import sys
import time
import traceback
import uuid
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
from mcp_client import LabVIEW, connect  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CHANNELS = 'Sim/ai0,Sim/ai1,Sim/ai2'
HEADER = 'time,ch1,ch2,ch3'
PARAMS = {'Setpoint': 10.0, 'Kp': 2.0, 'Ki': 0.25, 'Kd': 0.5,
          'Integral': 1.5, 'Previous Error': -0.5}
CASES = [[1.0, 2.0, 3.0, 4.0], [-2.5, 7.0, 12.0, 20.0], [9.5, 10.0, 10.5, -3.25]]
# output = Kp*error + (Integral + Ki*error) + Kd*(error-previous)
OPS = [('error', 'Subtract', 'x-y'), ('p', 'Multiply', 'x*y'),
       ('i', 'Multiply', 'x*y'), ('integral', 'Add', 'x+y'),
       ('delta', 'Subtract', 'x-y'), ('d', 'Multiply', 'x*y'),
       ('pi', 'Add', 'x+y'), ('pid', 'Add', 'x+y')]


class Trace(LabVIEW):
    def __init__(self, session, stream, stage):
        super().__init__(session)
        self.stream, self.stage, self.events = stream, stage, []

    async def call(self, name, **args):
        start = time.perf_counter()
        event = {'stage': self.stage, 'tool': name, 'args': args}
        try:
            value = await super().call(name, **args)
            event['result'] = value
            return value
        except Exception as exc:
            event['error'] = str(exc)
            raise
        finally:
            event['seconds'] = time.perf_counter() - start
            self.events.append(event)
            self.stream.write(json.dumps(event, ensure_ascii=False) + '\n')
            self.stream.flush()

    async def error(self, name, **args):
        try:
            await self.call(name, **args)
        except RuntimeError as exc:
            return str(exc)
        raise AssertionError(f'{name} unexpectedly succeeded')


def stats(events, elapsed):
    return {'wall_seconds': elapsed, 'mcp_calls': len(events),
            'by_tool': dict(Counter(e['tool'] for e in events)),
            'tool_seconds': sum(e['seconds'] for e in events)}


def ref(path):
    return {'$ref': path}


def resolve(value, results):
    if isinstance(value, dict):
        if set(value) == {'$ref'}:
            parts = value['$ref'].split('.')
            value = results[parts[0]]
            for part in parts[1:]:
                value = value[int(part)] if isinstance(value, list) else value[part]
            return value
        return {k: resolve(v, results) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, results) for v in value]
    return value


class Plan:
    def __init__(self):
        self.steps = []

    def add(self, tool, **args):
        name = f's{len(self.steps)}'
        self.steps.append({'id': name, 'tool': tool, 'args': args})
        return name

    def get(self, cls, member, reference):
        return ref(self.add('get_property', object_class=cls, member=member,
                            reference=reference) + '.value')

    def set(self, cls, member, reference, value):
        self.add('set_property', object_class=cls, member=member, reference=reference, value=value)

    def label(self, reference, label):
        self.set('Text', 'Text', self.get('Control', 'Label', reference), label)

    async def execute(self, lv, batch):
        if batch:
            result = await lv.call('edit_batch', steps=self.steps)
            assert len(result['completed']) == len(self.steps) and not result.get('failed'), result
            return result['results']
        results = {}
        for step in self.steps:
            result = await lv.call(step['tool'], **resolve(step['args'], results))
            # mcp_client unwraps get_property, whereas edit_batch preserves tool JSON.
            results[step['id']] = {'value': result} if step['tool'] == 'get_property' else result
        return results


async def create_branch(lv, vi, index_term, number, shared):
    nodes, terminals = {}, {}
    for j, (name, style, output) in enumerate(OPS):
        node = await lv.add(vi['diagram'], style, [350 + j * 90, 140 + number * 130])
        await lv.set('Text', 'Text', await lv.get('Node', 'Label', node), f'ch{number}_{name}')
        nodes[name] = node
        terminals[name] = await lv.terminals(node)
    links = branch_links(index_term, shared, terminals)
    for a, b in links:
        await lv.call('wire', source=a, destination=b)
    _, out = await lv.control(vi, f'Channel {number}', [500, 40 + number * 120], indicator=True)
    _, graph = await lv.control(vi, f'Chart {number}', [700, 40 + number * 180],
                                'Waveform Graph (modern)', indicator=True, bounds=[250, 150])
    arr = await lv.add(vi['diagram'], 'Build Array', [1150, 140 + number * 130])
    await lv.set('Text', 'Text', await lv.get('Node', 'Label', arr), f'ch{number}_plot')
    ts = (await lv.call('inspect_node', reference=arr))['terminals']
    await lv.call('wire', source=terminals['pid']['x+y'], destination=out)
    await lv.call('wire', source=terminals['pid']['x+y'],
                  destination=next(t['reference'] for t in ts if not t['source']))
    await lv.call('wire', source=next(t['reference'] for t in ts if t['source']), destination=graph)
    return nodes


def branch_links(measurement, shared, t):
    return [(shared['Setpoint'], t['error']['x']), (measurement, t['error']['y']),
            (t['error']['x-y'], t['p']['x']), (shared['Kp'], t['p']['y']),
            (t['error']['x-y'], t['i']['x']), (shared['Ki'], t['i']['y']),
            (t['i']['x*y'], t['integral']['x']), (shared['Integral'], t['integral']['y']),
            (t['error']['x-y'], t['delta']['x']), (shared['Previous Error'], t['delta']['y']),
            (t['delta']['x-y'], t['d']['x']), (shared['Kd'], t['d']['y']),
            (t['p']['x*y'], t['pi']['x']), (t['integral']['x+y'], t['pi']['y']),
            (t['pi']['x+y'], t['pid']['x']), (t['d']['x*y'], t['pid']['y'])]


async def build(lv, path):
    vi = await lv.call('new_vi', path=str(path))
    samples = await lv.add(vi['panel'], 'Array (modern)', [20, 20])
    await lv.add(samples, 'Numeric Control (modern)')
    await lv.set('Text', 'Text', await lv.get('Control', 'Label', samples), 'Samples')
    for member in ('Value', 'DefVal'):
        await lv.set('Control', member, samples, CASES[0])
    shared = {}
    for j, (name, value) in enumerate(PARAMS.items()):
        shared[name] = (await lv.control(vi, name, [20, 100 + j * 70], default=value))[1]
    for name, value, y in [('Channels', CHANNELS, 550), ('Header', HEADER, 620)]:
        await lv.control(vi, name, [20, y], 'String Control (modern)', default=value)
    index = await lv.add(vi['diagram'], 'Index Array', [200, 300])
    await lv.set('Text', 'Text', await lv.get('Node', 'Label', index), 'Channel Index')
    await lv.invoke('GrowableFunction', 'Resize.AddChunk', index, **{'Chunks To Add': 2})
    ts = (await lv.call('inspect_node', reference=index))['terminals']
    # The only non-index input is the array; indices remain unwired sequential defaults.
    inputs = [t for t in ts if not t['source']]
    await lv.call('wire', source=await lv.get('Control', 'Terminal', samples),
                  destination=inputs[0]['reference'])
    outputs = [t['reference'] for t in ts if t['source']]
    assert len(outputs) == 3, ts
    for number, terminal in enumerate(outputs, 1):
        await create_branch(lv, vi, terminal, number, shared)
    await lv.call('save_vi', path=str(path))
    return vi


async def values(lv, path, channels):
    answers = []
    names = [f'Channel {i}' for i in range(1, channels + 1)]
    plots = [f'Chart {i}' for i in range(1, channels + 1)]
    for samples in CASES:
        got = await lv.call('run_vi', path=str(path), inputs={'Samples': samples}, outputs=names + plots)
        for i in range(channels):
            error = PARAMS['Setpoint'] - samples[i]
            expected = PARAMS['Kp'] * error + PARAMS['Integral'] + PARAMS['Ki'] * error
            expected += PARAMS['Kd'] * (error - PARAMS['Previous Error'])
            assert math.isclose(got[names[i]], expected, abs_tol=1e-9), (got, expected)
            assert len(got[plots[i]]) == 1 and math.isclose(got[plots[i]][0], expected, abs_tol=1e-9), got
        answers.append(got)
    return answers


async def legacy_discovery(lv, path):
    """Actual pre-snapshot fine-grained discovery, including every wire endpoint."""
    vi = await lv.call('open_vi', path=str(path), reload=True)
    controls = []
    for r in await lv.call('find_objects', path=str(path), object_class='Control', panel=True):
        try:
            terminal = await lv.get('Control', 'Terminal', r)
        except RuntimeError as exc:
            if '1063' not in str(exc):
                raise
            continue
        label = await lv.get('Text', 'Text', await lv.get('Control', 'Label', r))
        controls.append({'reference': r, 'label': label,
                         'position': await lv.get('GObject', 'Position', r),
                         'terminal': terminal,
                         'value': await lv.get('Control', 'Value', r)})
    nodes = []
    for r in await lv.call('find_objects', path=str(path), object_class='Node'):
        node = await lv.call('inspect_node', reference=r)
        node['label'] = await lv.get('Text', 'Text', await lv.get('Node', 'Label', r))
        node['position'] = await lv.get('GObject', 'Position', r)
        for t in node['terminals']:
            t['wire'] = await lv.get('Terminal', 'Connected Wire', t['reference'])
            t['connections'] = (await lv.get('Wire', 'Terminals[]', t['wire'])) if t['wire'] else []
        nodes.append(node)
    wires = []
    for r in await lv.call('find_objects', path=str(path), object_class='Wire'):
        wires.append({'reference': r, 'terminals': await lv.get('Wire', 'Terminals[]', r),
                      'broken': await lv.get('Wire', 'Is Broken?', r)})
    return vi, {'nodes': nodes, 'controls': controls, 'wires': wires}


def objects(snapshot):
    return snapshot.get('objects', snapshot.get('nodes', []))


def labelled(snapshot, label):
    matches = {o['reference']: o for o in objects(snapshot) + snapshot.get('controls', [])
               if o.get('label') == label}
    assert len(matches) == 1, (label, list(matches.values()))
    return next(iter(matches.values()))


def control_terminal(control):
    if control.get('terminal'):
        return control['terminal']
    terminals = control.get('terminals', [])
    assert len(terminals) == 1, control
    return terminals[0]['reference']


def modification(vi, snapshot):
    p = Plan()
    index = labelled(snapshot, 'Channel Index')['reference']
    p.add('invoke', object_class='GrowableFunction', member='Resize.AddChunk', reference=index,
          inputs={'Chunks To Add': 1})
    fresh_index = p.add('inspect_node', reference=index)
    old_terms = labelled(snapshot, 'Channel Index')['terminals']
    # Resize appends one index/value pair; original ordering is captured, not assumed.
    old_sources = [j for j, t in enumerate(old_terms) if t['source']]
    assert len(old_sources) == 3, old_terms
    stride = old_sources[1] - old_sources[0]
    fourth = ref(f'{fresh_index}.terminals.{old_sources[-1] + stride}.reference')
    shared = {name: control_terminal(labelled(snapshot, name)) for name in PARAMS}
    terminals = {}
    for j, (name, style, output) in enumerate(OPS):
        node = ref(p.add('add_object', owner=vi['diagram'], style=style,
                         location=[350 + j * 90, 660]))
        p.set('Text', 'Text', p.get('Node', 'Label', node), f'ch4_{name}')
        inspected = p.add('inspect_node', reference=node)
        prototype = labelled(snapshot, f'ch1_{name}')['terminals']
        terminals[name] = {t['name']: ref(f'{inspected}.terminals.{i}.reference')
                           for i, t in enumerate(prototype)}
    for source, destination in branch_links(fourth, shared, terminals):
        p.add('wire', source=source, destination=destination)
    output_terms = []
    for label, style, xy in [('Channel 4', 'Numeric Control (modern)', [500, 520]),
                             ('Chart 4', 'Waveform Graph (modern)', [700, 760])]:
        control = ref(p.add('add_object', owner=vi['panel'], style=style, location=xy))
        p.label(control, label)
        p.set('Control', 'Indicator', control, True)
        output_terms.append(p.get('Control', 'Terminal', control))
    array = ref(p.add('add_object', owner=vi['diagram'], style='Build Array', location=[1150, 660]))
    p.set('Text', 'Text', p.get('Node', 'Label', array), 'ch4_plot')
    inspected = p.add('inspect_node', reference=array)
    prototype = labelled(snapshot, 'ch1_plot')['terminals']
    source = next(i for i, t in enumerate(prototype) if t['source'])
    dest = next(i for i, t in enumerate(prototype) if not t['source'])
    p.add('wire', source=terminals['pid']['x+y'], destination=output_terms[0])
    p.add('wire', source=terminals['pid']['x+y'],
          destination=ref(f'{inspected}.terminals.{dest}.reference'))
    p.add('wire', source=ref(f'{inspected}.terminals.{source}.reference'), destination=output_terms[1])
    for label, value in [('Channels', CHANNELS + ',Sim/ai3'), ('Header', HEADER + ',ch4')]:
        for member in ('Value', 'DefVal'):
            p.set('Control', member, labelled(snapshot, label)['reference'], value)
    p.add('save_vi', path=vi['path'])
    return p


def fingerprint(snapshot):
    """Semantic graph/layout hash: references are replaced with stable object identities."""
    all_objects = {o['reference']: o for o in objects(snapshot) + snapshot.get('controls', [])}
    identities = {r: json.dumps([o.get('class'), o.get('label'), o.get('position')],
                               sort_keys=True, ensure_ascii=False) for r, o in all_objects.items()}
    terms = {}
    records = []
    for r, o in all_objects.items():
        terminal_records = []
        for i, t in enumerate(o.get('terminals', [])):
            identity = (identities[r], t.get('name'), bool(t.get('source')), i)
            terms[t['reference']] = identity
            terminal_records.append(identity[1:])
        if o.get('terminal'):
            terms.setdefault(o['terminal'], (identities[r], 'control', None, 0))
        record = {'identity': identities[r], 'terminals': terminal_records}
        if 'value' in o and isinstance(o['value'], str):
            record['value'] = o['value']
        owner = o.get('owner')
        if owner in identities:
            record['owner'] = identities[owner]
        records.append(record)
    wires = []
    for wire in snapshot['wires']:
        assert wire.get('broken') is False, wire
        endpoints = []
        for terminal in wire['terminals']:
            r = terminal['reference'] if isinstance(terminal, dict) else terminal
            assert r in terms, ('snapshot omitted connected terminal', r)
            endpoints.append(terms[r])
        wires.append(sorted(endpoints, key=str))
    canonical = {'objects': sorted(records, key=lambda o: json.dumps(o, sort_keys=True)),
                 'wires': sorted(wires, key=str)}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


async def verify(lv, path, original):
    before = await lv.call('inspect_vi', path=str(path))
    assert before['wires'] and all(w.get('broken') is False for w in before['wires'])
    for label, expected in [('Channels', CHANNELS + ',Sim/ai3'), ('Header', HEADER + ',ch4')]:
        control = labelled(before, label)
        assert await lv.get('Control', 'Value', control['reference']) == expected
        assert await lv.get('Control', 'DefVal', control['reference']) == expected
    got = await values(lv, path, 4)
    for old, new in zip(original, got):
        assert all(new[k] == v for k, v in old.items()), (old, new)
    # Compare the edited structure itself to the saved/reloaded structure.
    digest = fingerprint(before)
    await lv.call('open_vi', path=str(path), reload=True)
    reopened = await lv.call('inspect_vi', path=str(path))
    assert digest == fingerprint(reopened), 'saved/reloaded structural fingerprint changed'
    again = await values(lv, path, 4)
    assert again == got, (again, got)
    return {'fingerprint': digest, 'cases': len(CASES), 'outputs': got,
            'wire_count': len(reopened['wires']), 'original_three_unchanged': True}


async def negative_paths(lv, path):
    vi = await lv.call('new_vi', path=str(path))
    before = await lv.call('inspect_vi', path=str(path))
    # A failing middle step must stop; its successor must not be silently executed.
    steps = [{'id': 'created', 'tool': 'add_object',
              'args': {'owner': vi['diagram'], 'style': 'Add'}},
             {'id': 'failed', 'tool': 'get_property',
              'args': {'object_class': 'GObject', 'member': 'NoSuchBenchmarkMember',
                       'reference': ref('created')}},
             {'id': 'not_run', 'tool': 'add_object',
              'args': {'owner': vi['diagram'], 'style': 'Multiply'}}]
    error = await lv.call('edit_batch', steps=steps)
    assert error.get('failed') == 'failed' and len(error['completed']) == 1, error
    assert error.get('error') and 'created' in error['results'] and 'not_run' not in error['results'], error
    after = await lv.call('inspect_vi', path=str(path))
    before_refs = {o['reference'] for o in objects(before)}
    created = [o for o in objects(after) if o['reference'] not in before_refs and o.get('class') == 'Function']
    assert len(created) == 1, ('batch failed to stop at middle error', created)
    await lv.call('delete_objects', references=[created[0]['reference']], clean_wires=True)
    deleted = await lv.call('inspect_vi', path=str(path))
    assert not any(o.get('class') == 'Function' for o in objects(deleted)), deleted
    invalid_error = await lv.error('delete_objects', references=[0], clean_wires=True)
    # Exercise removal of a connected node, not only a disconnected scratch object.
    a = await lv.add(vi['diagram'], 'Add', [100, 100])
    b = await lv.add(vi['diagram'], 'Multiply', [220, 100])
    at, bt = await lv.terminals(a), await lv.terminals(b)
    await lv.call('wire', source=at['x+y'], destination=bt['x'])
    await lv.call('delete_objects', references=[a, b], clean_wires=True)
    final = await lv.call('inspect_vi', path=str(path))
    assert not final['wires'], final['wires']
    await lv.call('save_vi', path=str(path))
    assert await lv.call('run_vi', path=str(path), inputs={}, outputs=[]) == {}
    return {'failed_batch': error, 'invalid_deletion': invalid_error,
            'stopped_before_successor': True, 'connected_deletion_executable': True}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    token = uuid.uuid4().hex[:12]
    out = args.output or ROOT / 'work' / f'modify_benchmark_{token}'
    out.mkdir(parents=True, exist_ok=True)
    report = {'output': str(out), 'protocol': {
        'setup': 'Four 3-channel VIs built from empty via fine-grained MCP in a separate unique worker namespace; per-fixture build and initial functional checks reported separately.',
        'cold': 'New server + unique per-method worker namespace; includes open, discovery, editing and save. Worker generation is charged to modification, never setup.',
        'hot': 'Fresh MCP server, same method cache namespace, separate pre-built equivalent VI.',
        'comparison': 'Identical dependent edit plans; baseline reads nodes/controls/wires individually and sends every edit individually; optimized reads one snapshot and submits one edit_batch.',
        'excluded': 'Fixture creation and functional/reload/negative verification are timed separately; no render, cleanup, artificial latency or cache deletion.',
        'timing': 'Wall-clock starts before MCP server initialization; MCP counts count outer requests, not internal batch steps.'},
        'runs': [], 'passed': False}
    try:
        with (out / 'raw.jsonl').open('w', encoding='utf-8') as raw:
            start = time.perf_counter()
            fixtures, originals = {}, {}
            async with connect(LABVIEW_WORKER_NAMESPACE=f'benchmark_setup_{token}') as connected:
                lv = Trace(connected.session, raw, 'setup')
                report['version'] = await lv.call('version_info')
                for mode in ('legacy', 'optimized'):
                    for temperature in ('cold', 'hot'):
                        key = f'{mode}_{temperature}'
                        path = out / f'Channels_{key}_{token}.vi'
                        fixtures[key] = path
                        build_start, offset = time.perf_counter(), len(lv.events)
                        await build(lv, path)
                        report.setdefault('builds', {})[key] = stats(lv.events[offset:], time.perf_counter() - build_start)
                        lv.stage = key + '_initial_verification'
                        verify_start, offset = time.perf_counter(), len(lv.events)
                        originals[key] = await values(lv, path, 3)
                        report.setdefault('initial_verification', {})[key] = stats(lv.events[offset:], time.perf_counter() - verify_start)
                        lv.stage = 'setup'
                report['setup'] = stats(lv.events, time.perf_counter() - start)
            for mode in ('legacy', 'optimized'):
                for temperature in ('cold', 'hot'):
                    key = f'{mode}_{temperature}'
                    path = fixtures[key]
                    start = time.perf_counter()
                    async with connect(LABVIEW_WORKER_NAMESPACE=f'benchmark_{mode}_{token}') as connected:
                        lv = Trace(connected.session, raw, key + '_modify')
                        info = await lv.call('version_info')
                        if mode == 'legacy':
                            vi, snapshot = await legacy_discovery(lv, path)
                        else:
                            vi = await lv.call('open_vi', path=str(path), reload=True)
                            snapshot = await lv.call('inspect_vi', path=str(path))
                        plan = modification(vi, snapshot)
                        await plan.execute(lv, batch=mode == 'optimized')
                        result = {'mode': mode, 'cache': temperature, 'version': info,
                                  'edit_steps': len(plan.steps),
                                  'modification': stats(lv.events, time.perf_counter() - start)}
                        result['editing_seconds'] = result['modification']['tool_seconds'] - sum(e['seconds'] for e in lv.events if e['tool'] == 'version_info')
                        result['startup_seconds'] = sum(e['seconds'] for e in lv.events if e['tool'] == 'version_info')
                        server_stats = await LabVIEW(connected.session).call('call_stats')
                        result['worker_events'] = [w for c in server_stats['calls'] for w in c['workers']]
                        result['workers_created'] = len(result['worker_events'])
                        report['runs'].append(result)
                        print(json.dumps({k: v for k, v in result.items() if k != 'worker_events'}, ensure_ascii=False), flush=True)
            # Verification uses a different namespace, so cold verification cannot
            # accidentally prewarm the hot modification server's worker set.
            async with connect(LABVIEW_WORKER_NAMESPACE=f'benchmark_verify_{token}') as connected:
                lv = Trace(connected.session, raw, 'verification')
                for result in report['runs']:
                    key = result['mode'] + '_' + result['cache']
                    offset, verify_start = len(lv.events), time.perf_counter()
                    lv.stage = key + '_verify'
                    result['verification'] = await verify(lv, fixtures[key], originals[key])
                    result['verification_cost'] = stats(lv.events[offset:], time.perf_counter() - verify_start)
            async with connect(LABVIEW_WORKER_NAMESPACE=f'benchmark_edges_{token}') as connected:
                lv = Trace(connected.session, raw, 'negative_paths')
                start = time.perf_counter()
                report['negative_paths'] = await negative_paths(lv, out / f'Edges_{token}.vi')
                report['negative_cost'] = stats(lv.events, time.perf_counter() - start)
        report['comparison'] = {}
        for temperature in ('cold', 'hot'):
            baseline, optimized = [next(r for r in report['runs'] if r['mode'] == mode and r['cache'] == temperature)
                                   for mode in ('legacy', 'optimized')]
            assert baseline['verification']['outputs'] == optimized['verification']['outputs'], 'edit modes produce different outputs'
            assert baseline['edit_steps'] == optimized['edit_steps'], 'edit modes apply different operation counts'
            report['comparison'][temperature] = {
                'call_reduction': baseline['modification']['mcp_calls'] / optimized['modification']['mcp_calls'],
                'wall_speedup': baseline['modification']['wall_seconds'] / optimized['modification']['wall_seconds'],
                'editing_speedup': baseline['editing_seconds'] / optimized['editing_seconds']}
        report['passed'] = True
    except Exception:
        report['error'] = traceback.format_exc()
        print(report['error'], file=sys.stderr, flush=True)
    finally:
        (out / 'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
        text = json.dumps({'passed': report['passed'], 'report': str(out / 'report.json'),
                           'raw': str(out / 'raw.jsonl'), 'comparison': report.get('comparison'),
                           'error': report.get('error')}, indent=2, ensure_ascii=False)
        (out / 'summary.txt').write_text(text + '\n', encoding='utf-8')
        print(text, flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
