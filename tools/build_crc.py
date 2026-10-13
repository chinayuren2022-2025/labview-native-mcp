"""Build CRC16_CCITT_FALSE.vi natively through VI Scripting over COM.

Controls are native U8[] / U16, not OLE Variant controls created from COM values.
Formula terminal Position is scalar; connector pattern 4815 uses indices 0 / 3.
Saved/reopened VI verified with 123456789 -> 0x29B1, empty -> 0xFFFF,
00 -> 0xE1F0 and FF -> 0xFF00. Outputs go only to work/crc_replica.
"""
from lvscript import Session, ROOT

OUT = ROOT / 'work' / 'crc_replica' / 'CRC16_CCITT_FALSE.vi'
FORMULA = """int32 i, b, n, c, v;
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
CONPANE = [('property', 'VI', 'ConPane.Ref', False)]


def step(msg):
    print('-', msg, flush=True)


def fp_object(s, panel, label, term_pos, panel_pos, indicator=False):
    """Create native U8[] input or U16 output, avoiding OLE Variant data types."""
    style = 'Numeric Indicator (modern)' if indicator else 'Array (modern)'
    obj = s.new_object(panel, style, location=panel_pos)
    numeric = obj if indicator else s.new_object(obj, 'Numeric Control (modern)')
    s.prop_set('Numeric', 'Rep', numeric, 7 if indicator else 8)
    s.prop_set('Text', 'Text', s.prop_get('Control', 'Label', obj), label)
    s.prop_set('GObject', 'Position', obj, panel_pos)
    term = s.prop_get('Control', 'Terminal', obj)
    s.prop_set('GObject', 'Position', term, term_pos)
    return obj, term


def build(s, out=OUT, terminal_index=(0, None)):
    out.parent.mkdir(parents=True, exist_ok=True)
    target = s.new_vi(out)
    top = s.diagram(target)

    step('formula node')
    formula = s.new_object(top, 'Formula Node', location=(444, 118), bounds=(249, 271))
    s.prop_set('Formula', 'FormExpr', formula, FORMULA)
    data_in = s.method('Formula', 'Add Input', formula, Position=37, Name='data')['Add Input']
    crc_out = s.method('Formula', 'Add Output', formula, Position=217, Name='crc')['Add Output']
    data_in = s.prop_get('FormulaParameter', 'Terminal', data_in)
    crc_out = s.prop_get('FormulaParameter', 'Terminal', crc_out)

    step('front panel objects')
    panel = s.prop_get('VI', 'Panel', s.vi(target))
    data_ctl, data_term = fp_object(s, panel, 'data', (362, 146), (-49, -5))
    crc_ind, crc_term = fp_object(s, panel, 'crc', (701, 326), (374, 0), indicator=True)

    step('wires')
    s.method('Terminal', 'Connect Wire', data_in, **{'Wire Source': data_term})
    s.method('Terminal', 'Connect Wire', crc_term, **{'Wire Source': crc_out})

    step('connector pane')
    vi_ref = s.vi(target)
    s.chain(CONPANE + [('property', 'ConnectorPane', 'Pattern', True)], vi_ref, Pattern=4815)
    assign = CONPANE + [('method', 'ConnectorPane', 'AssignCtrlToTerm', False)]
    data_idx, crc_idx = terminal_index
    s.chain(assign, vi_ref, Control=data_ctl, **{'Terminal Index': data_idx})
    if crc_idx is not None:
        s.chain(assign, vi_ref, Control=crc_ind, **{'Terminal Index': crc_idx})

    s.save(target)
    print('saved', out)
    return target


if __name__ == '__main__':
    import sys
    s = Session()
    idx = tuple(int(x) for x in sys.argv[1:3]) if len(sys.argv) > 2 else (0, 3)
    t = build(s, terminal_index=idx)
    print(s.render(t, ROOT / 'work' / 'lvs_render' / 'crc_replica'))
