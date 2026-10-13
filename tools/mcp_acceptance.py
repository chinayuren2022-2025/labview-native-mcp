"""Exercise real stdio MCP tools against LabVIEW; builds a fresh CRC VI."""
import asyncio
import base64
import json
import sys
import tempfile
from pathlib import Path
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
from build_crc import FORMULA


async def main():
    out = Path(tempfile.mkdtemp(prefix='labview-mcp-')) / 'CRC16_MCP.vi'
    server = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).with_name('labview_mcp.py'))])
    async with stdio_client(server) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            await client.initialize()
            async def call(name, **args):
                result = await client.call_tool(name, args)
                if result.isError:
                    raise RuntimeError(f'{name}: {result.content}')
                if result.structuredContent is not None:
                    obj = result.structuredContent
                    obj = obj['result'] if set(obj) == {'result'} else obj
                    return obj['value'] if name == 'get_property' else obj
                obj = json.loads(next(c.text for c in result.content if c.type == 'text'))
                return obj['value'] if name == 'get_property' else obj
            tools = await client.list_tools()
            print('tools', [t.name for t in tools.tools], flush=True)
            from lvscript import LV, Session
            # Make an owned instance through LabVIEW before starting MCP COM calls.
            express_path=out.with_name('Express_Simulate_owned.vi')
            native=Session()
            native.save(LV/'vi.lib'/'express'/'express input'/'SimulateSignalBlock.llb'/'Ex_Inst_Simulate Signal.vi',express_path)
            native._invoke(native.vi(express_path),'DisconnectFromLibrary')
            native.save(express_path)
            advanced = await call('new_vi',path=str(out.with_name('AdvancedEditing.vi')))
            poly = await call('add_object',owner=advanced['diagram'],path=str(LV/'vi.lib'/'Bit Manipulation'/'Little-Endian Bytes To Words Poly.vi'))
            first = await call('select_polymorphic',reference=poly,instance_index=2)
            second = await call('select_polymorphic',reference=poly,instance_index=4)
            assert first['selected']==2 and second['selected']==4
            inp = await call('invoke',object_class='Terminal',member='Create Control',reference=second['terminals']['bytes in'])
            output = await call('invoke',object_class='Terminal',member='Create Indicator',reference=second['terminals']['words out'])
            for ref,name in [(inp['Create Control'],'bytes'),(output['Create Indicator'],'words')]:
                label = await call('get_property',object_class='Control',member='Label',reference=ref)
                await call('set_property',object_class='Text',member='Text',reference=label,value=name)
            await call('save_vi',path=advanced['path'])
            result=await call('run_vi',path=advanced['path'],inputs={'bytes':{'type':'u8_array','value':[1,2,3,4]}},outputs=['words'])
            assert result['words']==[513,1027],result
            express=await call('configure_express_parameters',instance_path=str(express_path),
                configuration_path=str(LV/'vi.lib'/'express'/'express input'/'SimulateSignalConfig.llb'/'Configure Simulate Signal.vi'),
                mappings={'Amplitude':'Amplitude'},values={'Amplitude':2.5})
            assert express['values']['Amplitude']==2.5,express
            print('advanced editing MCP gate passed; rebuilding CRC now',flush=True)
            vi = await call('new_vi', path=str(out))
            formula = await call('add_object', owner=vi['diagram'], style='Formula Node', location=[444,118], bounds=[249,271])
            await call('set_property', object_class='Formula', member='FormExpr', reference=formula, value=FORMULA)
            inp = await call('invoke', object_class='Formula', member='Add Input', reference=formula, inputs={'Position':37,'Name':'data'})
            output = await call('invoke', object_class='Formula', member='Add Output', reference=formula, inputs={'Position':217,'Name':'crc'})
            data_in = await call('get_property', object_class='FormulaParameter',member='Terminal',reference=inp['Add Input'])
            crc_out = await call('get_property', object_class='FormulaParameter',member='Terminal',reference=output['Add Output'])
            data = await call('add_object',owner=vi['panel'],style='Array (modern)',location=[0,0])
            elem = await call('add_object',owner=data,style='Numeric Control (modern)')
            crc = await call('add_object',owner=vi['panel'],style='Numeric Indicator (modern)',location=[300,0])
            for reference,rep in [(elem,8),(crc,7)]:
                await call('set_property',object_class='Numeric',member='Rep',reference=reference,value=rep)
            terminals=[]
            for reference,label in [(data,'data'),(crc,'crc')]:
                text = await call('get_property',object_class='Control',member='Label',reference=reference)
                await call('set_property',object_class='Text',member='Text',reference=text,value=label)
                terminals.append(await call('get_property',object_class='Control',member='Terminal',reference=reference))
            await call('wire',source=terminals[0],destination=data_in)
            await call('wire',source=crc_out,destination=terminals[1])
            await call('connector',path=str(out),pattern=4815,assignments={'0':data,'3':crc})
            await call('save_vi',path=str(out))
            for data_bytes,expected in [(b'123456789',0x29b1),(b'',0xffff),(b'\x00',0xe1f0),(b'\xff',0xff00)]:
                result=await call('run_vi',path=str(out),inputs={'data':{'type':'u8_array','value':list(data_bytes)}},outputs=['crc'])
                assert result['crc']==expected,(result,expected)
                print('CRC',data_bytes,hex(result['crc']),flush=True)
            # Mode change affects live terminal direction, not merely a stored flag.
            before = await call('get_property',object_class='Terminal',member='Is Source?',reference=terminals[1])
            await call('set_property',object_class='Control',member='Indicator',reference=crc,value=False)
            changed = await call('get_property',object_class='Control',member='Terminal',reference=crc)
            after = await call('get_property',object_class='Terminal',member='Is Source?',reference=changed)
            assert before is False and after is True,(before,after)
            await call('set_property',object_class='Control',member='Indicator',reference=crc,value=True)
            print('control direction transition passed',flush=True)
            image=await client.call_tool('render_vi',{'path':str(out)})
            assert not image.isError,image.content
            png=next(c for c in image.content if c.type=='image')
            image_path=out.with_suffix('.png')
            image_path.write_bytes(base64.b64decode(png.data))
            print('image',image_path,'mime',png.mimeType,flush=True)
            print('VI',out,flush=True)
            config = await call('new_vi', path=str(out.with_name('NodeModes.vi')))
            array_node = await call('add_object',owner=config['diagram'],style='Build Array')
            before_node = await call('inspect_node',reference=array_node)
            await call('invoke',object_class='GrowableFunction',member='Resize.AddChunk',reference=array_node,
                       inputs={'Chunks To Add':2})
            after_node = await call('inspect_node',reference=array_node)
            assert sum(not t['source'] for t in after_node['terminals']) == sum(not t['source'] for t in before_node['terminals']) + 2
            case = await call('add_object',owner=config['diagram'],style='Case Structure',location=[300,100])
            frames = await call('get_property',object_class='MultiFrameStructure',member='Frames[]',reference=case)
            added = await call('invoke',object_class='MultiFrameStructure',member='Add Frame',reference=case)
            new_frames = await call('get_property',object_class='MultiFrameStructure',member='Frames[]',reference=case)
            new_frame = next(f for f in new_frames if f not in frames)
            inner = await call('add_object',owner=new_frame,style='Add')
            assert (await call('inspect_node',reference=inner))['class'] == 'Function'
            prop = await call('add_object',owner=config['diagram'],style='Property Node',location=[600,100])
            await call('set_property',object_class='Property',member='PropClassName',reference=prop,value='VI Server:Control')
            items = await call('invoke',object_class='Property',member='Set Properties[]',reference=prop,
                               inputs={'ID Strings[]':['633200d']})
            refs = await call('get_property',object_class='Property',member='Property Items[]',reference=prop)
            item = refs[0]
            await call('set_property',object_class='PropertyItem',member='IsWrite',reference=item,value=True)
            assert await call('get_property',object_class='PropertyItem',member='IsWrite',reference=item) is True
            await call('set_property',object_class='PropertyItem',member='IsWrite',reference=item,value=False)
            assert await call('get_property',object_class='PropertyItem',member='IsWrite',reference=item) is False
            await call('save_vi',path=config['path'])
            print('dynamic terminals, case frame editing, property read/write passed',flush=True)


if __name__ == '__main__':
    asyncio.run(main())
