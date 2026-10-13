"""Post-install smoke test: start the MCP server over stdio, create a VI with a native node, save it."""
import asyncio
import tempfile
from pathlib import Path

from mcp_client import connect


async def main():
    path = str(Path(tempfile.mkdtemp(prefix='lv-mcp-install-')) / 'Probe.vi')
    async with connect() as lv:
        vi = await lv.call('new_vi', path=path)
        node = await lv.add(vi['diagram'], 'Add')
        assert (await lv.call('inspect_node', reference=node))['class'] == 'Function'
        await lv.call('save_vi', path=path)
    print('INSTALL_PROBE_OK: real MCP session, native Add node creation and save')


if __name__ == '__main__':
    asyncio.run(main())
