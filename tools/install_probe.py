"""Probe actual stdio MCP and native object creation after isolated installation."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters

async def main():
    params=StdioServerParameters(command=sys.executable,args=[str(Path(__file__).with_name('labview_mcp.py'))])
    async with stdio_client(params) as (reader,writer):
        async with ClientSession(reader,writer) as client:
            await client.initialize()
            async def call(name,**args):
                result=await client.call_tool(name,args)
                if result.isError:raise RuntimeError(f'{name}: {result.content}')
                obj=result.structuredContent
                if obj is None:obj=json.loads(next(x.text for x in result.content if x.type=='text'))
                return obj['result'] if isinstance(obj,dict) and set(obj)=={'result'} else obj
            path=str(Path(tempfile.mkdtemp(prefix='lv-mcp-install-'))/'Probe.vi')
            vi=await call('new_vi',path=path)
            node=await call('add_object',owner=vi['diagram'],style='Add')
            info=await call('inspect_node',reference=node)
            assert info['class']=='Function',info
            await call('save_vi',path=path)
            print('INSTALL_PROBE_OK: real MCP session, native Add node creation and save')

if __name__=='__main__':asyncio.run(main())
