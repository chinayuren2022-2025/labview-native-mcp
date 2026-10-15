"""Read native VI structure in one STA operation; refs are session-local, UIDs identify objects."""
from lvscript import LabVIEWError


def snapshot(s, path, inspect_node):
    issues, identities, aliases, uids, cache = [], {}, {}, {}, {}
    def get(cls, member, ref, default=None):
        key = (cls, member, ref)
        if key in cache:
            return cache[key]
        try:
            value = s.prop_get(cls, member, ref)
            cache[key] = value
            return value
        except LabVIEWError as exc:
            issues.append({'reference': ref, 'member': f'{cls}.{member}', 'error': str(exc)})
            return default
    def preload(cls, member, refs):
        refs = list(dict.fromkeys(r for r in refs if r))
        for ref, value in zip(refs, s.prop_many(cls, member, refs)):
            cache[cls, member, ref] = value
    def canonical(ref):
        if not ref:
            return ref
        if ref in aliases:
            return aliases[ref]
        uid = get('GObject', 'UID', ref)
        uids[ref] = uid
        aliases[ref] = identities.setdefault(uid, ref)
        return aliases[ref]
    diagram_ref = s.diagram(path)
    panel_ref = s.prop_get('VI', 'Panel', s.vi(path))
    raw_controls = s.find(path, 'Control', target=0)
    raw = list(dict.fromkeys([diagram_ref, panel_ref] + raw_controls +
                            s.find(path, 'GObject') + s.find(path, 'GObject', target=0)))
    preload('GObject', 'UID', raw)
    diagram, panel = canonical(diagram_ref), canonical(panel_ref)
    control_refs = {canonical(r) for r in raw_controls}
    refs = list(dict.fromkeys(canonical(r) for r in raw))
    preload('GObject', 'Class Name', refs)
    preload('GObject', 'Left', refs)
    preload('GObject', 'Top', refs)
    objects, terminals, controls, wires = {}, {}, [], []
    for ref in refs:
        cls = get('GObject', 'Class Name', ref)
        obj = {'reference': ref, 'uid': uids[ref], 'class': cls,
               'owner': canonical(get('GObject', 'Owner', ref)),
               'position': [get('GObject', 'Left', ref), get('GObject', 'Top', ref)], 'terminals': []}
        objects[ref] = obj
        if cls == 'Wire':
            endpoints = s.prop_get('Wire', 'Terminals[]', ref)
            preload('GObject', 'UID', endpoints)
            wires.append({'reference': ref, 'terminals': [canonical(t) for t in endpoints],
                          'broken': s.prop_get('Wire', 'Is Broken?', ref)})
            continue
        if ref in control_refs:
            labelref = get('Control', 'Label', ref)
            obj['label'] = get('Text', 'Text', labelref) if labelref else ''
            obj['terminal'] = canonical(get('Control', 'Terminal', ref))
            obj['indicator'] = get('Control', 'Indicator', ref)
            obj['value'] = get('Control', 'Value', ref)
            controls.append(obj)
            continue
        if cls in ('Terminal', 'OuterTerminal', 'InnerTerminal', 'ControlTerminal',
                   'Panel', 'Pane', 'Text', 'Decoration', 'Label') or cls.endswith('Diagram'):
            continue
        try:
            info = inspect_node(s, ref)
        except LabVIEWError as exc:
            obj['inspection_error'] = str(exc)
            continue
        preload('GObject', 'UID', [t['reference'] for t in info['terminals']])
        for terminal in info['terminals']:
            terminal['reference'] = canonical(terminal['reference'])
            terminals[terminal['reference']] = terminal
        obj['terminals'] = info['terminals']
        if cls.endswith('Constant'):
            obj['value'] = get('Constant', 'Value', ref)
            labelref = get('Constant', 'Label', ref)
        else:
            labelref = get('Node', 'Label', ref)
            obj['style'] = get('Node', 'Style', ref)
        obj['label'] = get('Text', 'Text', labelref) if labelref else ''
    for obj in controls:
        ref = obj.get('terminal')
        if ref:
            terminals.setdefault(ref, {'reference': ref, 'name': obj['label'],
                                       'source': not obj['indicator'], 'role': 'control'})
            obj['terminals'] = [terminals[ref]]
    for wire in wires:
        for ref in wire['terminals']:
            if ref not in terminals:
                terminals[ref] = {'reference': ref, 'name': get('Terminal', 'Name', ref),
                                  'source': get('Terminal', 'Is Source?', ref),
                                  'owner': canonical(get('GObject', 'Owner', ref)), 'role': 'terminal'}
            terminals[ref]['wire'] = wire['reference']
            terminals[ref]['connections'] = [t for t in wire['terminals'] if t != ref]
    for term in terminals.values():
        term.setdefault('wire', 0)
        term.setdefault('connections', [])
    return {'path': str(path), 'diagram': diagram, 'panel': panel,
            'objects': list(objects.values()),
            'nodes': [o for o in objects.values() if o['terminals'] and o not in controls],
            'controls': controls, 'terminals': list(terminals.values()), 'wires': wires,
            'issues': issues, 'executable': s.prop_get('VI', 'Exec.State', s.vi(path)) == 1}
