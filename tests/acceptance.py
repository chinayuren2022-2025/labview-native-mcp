"""End-to-end acceptance over real stdio MCP: polymorphic/Express editing, a CRC-16
VI built from scratch and checked against standard vectors, dynamic terminals,
case frames and property items. Only temporary VIs are touched."""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
from lvscript import LV, Session  # noqa: E402
from mcp_client import connect  # noqa: E402

CRC = """int32 i, b, n, c, v;
n = sizeOfDim(data, 0);
c = 65535;
for (i=0; i<n; i++) {
  v = data[i];
  c = c ^ (v << 8);
  for (b=0; b<8; b++) {
    if ((c & 32768) != 0)
      c = (c << 1) ^ 4129;
    else c = c << 1;
    c = c & 65535;
  }
}
crc = c;"""
U8 = lambda values: {'type': 'u8_array', 'value': list(values)}  # noqa: E731


async def main():
    root = Path(tempfile.mkdtemp(prefix='labview-mcp-'))
    # Express instance must be owned (disconnected from its library) before configuring it.
    express_path = root / 'Express_Simulate_owned.vi'
    native = Session()
    native.save(LV / 'vi.lib/express/express input/SimulateSignalBlock.llb/Ex_Inst_Simulate Signal.vi', express_path)
    native._invoke(native.vi(express_path), 'DisconnectFromLibrary')
    native.save(express_path)

    async with connect() as lv:
        print('tools', [t.name for t in (await lv.session.list_tools()).tools], flush=True)

        async def label(ref, name):
            await lv.set('Text', 'Text', await lv.get('Control', 'Label', ref), name)

        vi = await lv.call('new_vi', path=str(root / 'Polymorphic.vi'))
        poly = await lv.call('add_object', owner=vi['diagram'],
                             path=str(LV / 'vi.lib/Analysis/baseanly.llb/Mean.vi'))   # in 2018 and 2021
        assert (await lv.call('select_polymorphic', reference=poly, instance_index=1))['selected'] == 1   # CDB
        node = await lv.call('select_polymorphic', reference=poly, instance_index=0)                       # DBL
        assert node['selected'] == 0
        for member, terminal, name in [('Create Control', 'X', 'x'), ('Create Indicator', 'mean', 'mean')]:
            await label((await lv.invoke('Terminal', member, node['terminals'][terminal]))[member], name)
        await lv.call('save_vi', path=vi['path'])
        assert (await lv.call('run_vi', path=vi['path'], inputs={'x': [1.0, 2.0, 3.0, 6.0]}, outputs=['mean']))['mean'] == 3.0
        express = await lv.call('configure_express_parameters', instance_path=str(express_path),
                                configuration_path=str(LV / 'vi.lib/express/express input/SimulateSignalConfig.llb/Configure Simulate Signal.vi'),
                                mappings={'Amplitude': 'Amplitude'}, values={'Amplitude': 2.5})
        assert express['values']['Amplitude'] == 2.5, express
        print('polymorphic + Express passed', flush=True)

        path = root / 'CRC16.vi'
        vi = await lv.call('new_vi', path=str(path))
        term = await lv.formula(vi['diagram'], CRC, ['data'], ['crc'], [444, 118], [249, 271])
        data = await lv.add(vi['panel'], 'Array (modern)', [0, 0])
        element = await lv.add(data, 'Numeric Control (modern)')
        crc = await lv.add(vi['panel'], 'Numeric Indicator (modern)', [300, 0])
        await lv.set('Numeric', 'Rep', element, 8)   # U8
        await lv.set('Numeric', 'Rep', crc, 7)       # U16
        await label(data, 'data')
        await label(crc, 'crc')
        crc_terminal = await lv.get('Control', 'Terminal', crc)
        await lv.call('wire', source=await lv.get('Control', 'Terminal', data), destination=term['data'])
        await lv.call('wire', source=term['crc'], destination=crc_terminal)
        await lv.call('connector', path=str(path), pattern=4815, assignments={'0': data, '3': crc})
        await lv.call('save_vi', path=str(path))
        for message, expected in [(b'123456789', 0x29B1), (b'', 0xFFFF), (b'\x00', 0xE1F0), (b'\xff', 0xFF00)]:
            result = await lv.call('run_vi', path=str(path), inputs={'data': U8(message)}, outputs=['crc'])
            assert result['crc'] == expected, (message, result, hex(expected))
        assert (await lv.png(path)).startswith(b'\x89PNG')
        print('CRC-16/CCITT-FALSE vectors passed', flush=True)

        # Switching control/indicator changes the live terminal direction.
        assert await lv.get('Terminal', 'Is Source?', crc_terminal) is False
        await lv.set('Control', 'Indicator', crc, False)
        assert await lv.get('Terminal', 'Is Source?', await lv.get('Control', 'Terminal', crc)) is True
        await lv.set('Control', 'Indicator', crc, True)

        vi = await lv.call('new_vi', path=str(root / 'NodeModes.vi'))
        array = await lv.add(vi['diagram'], 'Build Array')
        inputs = lambda info: sum(not t['source'] for t in info['terminals'])  # noqa: E731
        before = inputs(await lv.call('inspect_node', reference=array))
        await lv.invoke('GrowableFunction', 'Resize.AddChunk', array, **{'Chunks To Add': 2})
        assert inputs(await lv.call('inspect_node', reference=array)) == before + 2
        case = await lv.add(vi['diagram'], 'Case Structure', [300, 100])
        frames = await lv.get('MultiFrameStructure', 'Frames[]', case)
        await lv.invoke('MultiFrameStructure', 'Add Frame', case)
        frame = next(f for f in await lv.get('MultiFrameStructure', 'Frames[]', case) if f not in frames)
        assert (await lv.call('inspect_node', reference=await lv.add(frame, 'Add')))['class'] == 'Function'
        prop = await lv.add(vi['diagram'], 'Property Node', [600, 100])
        await lv.set('Property', 'PropClassName', prop, 'VI Server:Control')
        await lv.invoke('Property', 'Set Properties[]', prop, **{'ID Strings[]': ['633200d']})
        item = (await lv.get('Property', 'Property Items[]', prop))[0]
        for value in (True, False):
            await lv.set('PropertyItem', 'IsWrite', item, value)
            assert await lv.get('PropertyItem', 'IsWrite', item) is value
        await lv.call('save_vi', path=vi['path'])
        print('dynamic terminals, case frames, property items passed', flush=True)
    print('ACCEPTANCE_OK', root)


if __name__ == '__main__':
    asyncio.run(main())
