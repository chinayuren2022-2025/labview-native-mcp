"""LabVIEW 2021 and 2018 side by side: two servers at once, each building, running and reloading a VI.

Needs both versions installed. Each server is pinned with LABVIEW_VERSION and must report
the LabVIEW it actually reached through COM.
"""
import asyncio
import sys
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from mcp_client import connect  # noqa: E402


async def build(lv, path):
    vi = await lv.call('new_vi', path=str(path))
    _, n = await lv.control(vi, 'Count', [30, 40], default=7)
    _, x = await lv.control(vi, 'Offset', [30, 110], default=0.5)
    _, y = await lv.control(vi, 'Last Value', [300, 40], indicator=True)
    loop = await lv.add(vi['diagram'], 'For Loop', [200, 100], [400, 220])
    lt = await lv.terminals(loop)
    at = await lv.terminals(await lv.add(await lv.get('Loop', 'Diagram', loop), 'Add', [300, 180]))
    for source, destination in [(n, lt['N']), (lt['i'], at['x']), (x, at['y']), (at['x+y'], y)]:
        await lv.call('wire', source=source, destination=destination)
    for tunnel in await lv.call('find_objects', path=vi['path'], object_class='LoopTunnel'):
        await lv.set('LoopTunnel', 'IndexMode', tunnel, 0)   # last value, not an array
    await lv.call('save_vi', path=vi['path'])
    return vi['path']


async def main():
    root = Path(tempfile.mkdtemp(prefix='labview-mcp-multiversion-'))
    async with AsyncExitStack() as stack:
        servers = {}
        for year in (2021, 2018):
            lv = await stack.enter_async_context(connect(LABVIEW_HOME=None, LABVIEW_VERSION=str(year)))
            info = await lv.call('version_info')
            assert info['year'] == year and info['actual_version'].startswith(str(year - 2000)), info
            servers[year] = lv
        paths = await asyncio.gather(*(build(lv, root / f'Sum_{year}.vi') for year, lv in servers.items()))
        for (year, lv), path in zip(servers.items(), paths):
            for count, offset in [(7, 0.5), (1, -3.0), (17, 2.25)]:
                out = await lv.call('run_vi', path=path, inputs={'Count': count, 'Offset': offset}, outputs=['Last Value'])
                assert out['Last Value'] == count - 1 + offset, (year, out)
            await lv.call('open_vi', path=path, reload=True)   # run what was saved, not the in-memory copy
            out = await lv.call('run_vi', path=path, inputs={'Count': 7, 'Offset': 0.5}, outputs=['Last Value'])
            assert out['Last Value'] == 6.5, (year, out)
            print(f'ok LabVIEW {year}: built, ran 3 cases, reloaded from disk', flush=True)
    print('MULTIVERSION_OK', root)


if __name__ == '__main__':
    asyncio.run(main())
