"""Small stdio MCP client shared by the bundled tests and examples."""
import base64
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

SERVER = Path(__file__).with_name('labview_mcp.py')


class LabVIEW:
    def __init__(self, session):
        self.session = session

    async def call(self, name, **args):
        """Call a tool and return its plain JSON value; tool errors raise RuntimeError."""
        result = await self.session.call_tool(name, args)
        if result.isError:
            raise RuntimeError(f'{name}: ' + ' '.join(getattr(c, 'text', '') for c in result.content))
        value = result.structuredContent
        if value is None:
            value = json.loads(next(c.text for c in result.content if c.type == 'text'))
        if isinstance(value, dict) and set(value) == {'result'}:
            value = value['result']
        return value['value'] if name == 'get_property' else value

    async def error(self, name, **args):
        """Call a tool that is expected to fail and return the error text."""
        result = await self.session.call_tool(name, args)
        assert result.isError, f'{name} unexpectedly succeeded'
        return '\n'.join(c.text for c in result.content if c.type == 'text')

    async def get(self, cls, member, ref):
        return await self.call('get_property', object_class=cls, member=member, reference=ref)

    async def set(self, cls, member, ref, value):
        return await self.call('set_property', object_class=cls, member=member, reference=ref, value=value)

    async def invoke(self, cls, member, ref, **inputs):
        return await self.call('invoke', object_class=cls, member=member, reference=ref, inputs=inputs)

    async def add(self, owner, style, xy=None, bounds=None):
        return await self.call('add_object', owner=owner, style=style, location=xy, bounds=bounds)

    async def terminals(self, ref):
        """Terminal refs of a node/loop/shift register keyed by (semantic) name."""
        return {t['name']: t['reference'] for t in (await self.call('inspect_node', reference=ref))['terminals']}

    async def control(self, vi, name, xy, style='Numeric Control (modern)', indicator=False,
                      default=None, bounds=None):
        """Place a labelled panel object and return (control ref, diagram terminal ref)."""
        ref = await self.add(vi['panel'], style, xy, bounds)
        await self.set('Text', 'Text', await self.get('Control', 'Label', ref), name)
        if indicator:
            await self.set('Control', 'Indicator', ref, True)
        if default is not None:
            await self.set('Control', 'Value', ref, default)
            await self.set('Control', 'DefVal', ref, default)
        return ref, await self.get('Control', 'Terminal', ref)

    async def formula(self, owner, code, inputs, outputs, xy, bounds):
        """Formula Node with named inputs/outputs; returns its terminals by name."""
        node = await self.add(owner, 'Formula Node', xy, bounds)
        for member, names in (('Add Input', inputs), ('Add Output', outputs)):
            for j, name in enumerate(names):
                await self.invoke('Formula', member, node, Position=20 + j * 24, Name=name)
        await self.set('Formula', 'FormExpr', node, code)
        return await self.terminals(node)

    async def png(self, path, surface='diagram'):
        result = await self.session.call_tool('render_vi', {'path': str(path), 'surface': surface})
        if result.isError:
            raise RuntimeError(result.content)
        return base64.b64decode(next(c.data for c in result.content if c.type == 'image'))


@asynccontextmanager
async def connect(**overrides):
    """Start the server. LABVIEW_HOME / LABVIEW_VERSION pass through unless overridden; None drops one."""
    env = {k: os.environ[k] for k in ('LABVIEW_HOME', 'LABVIEW_VERSION', 'LABVIEW_WORKER_NAMESPACE', 'LABVIEW_CALL_LOG') if k in os.environ}
    env = {k: v for k, v in {**env, **overrides}.items() if v is not None}
    server = StdioServerParameters(command=sys.executable, args=[str(SERVER)], env=env)
    async with stdio_client(server) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            yield LabVIEW(session)
