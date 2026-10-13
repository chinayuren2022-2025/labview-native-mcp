"""Native LabVIEW 2021 block-diagram scripting over COM (no DLL injection).

Every VI Server scripting member (invoke method / property) is reached through a
generated "worker" VI: a blank VI holding one configured Invoke/Property Node whose
terminals were auto-wired to controls by Quick Drop's Ctrl+D core. Workers are cached
under work/workers and driven through COM by control name.

GObject refnums (diagrams, nodes, terminals) cross COM as plain ints and stay valid
for the lifetime of the LabVIEW session.
"""
import os
import re
import shutil
import tempfile
from pathlib import Path

import pythoncom
import win32com.client

LV = Path(os.environ.get('LABVIEW_HOME', r'C:\Program Files\National Instruments\LabVIEW 2021'))
ROOT = Path(__file__).resolve().parent.parent
LVS = ROOT / 'work' / 'lvscript'          # patched Erdos Miller LV-Scripting copy
WORKERS = ROOT / 'work' / 'workers'
BLANK = WORKERS / '_blank.vi'
NO_ERR = (False, 0, '')

DISPID_GET_VI_REFERENCE = 2004
DISPID_CALL = 1015
METHOD = pythoncom.DISPATCH_METHOD


def cluster_array(rows):
    """1-D array of clusters (pywin32 would otherwise marshal a list of tuples as a 2-D array)."""
    v = win32com.client.VARIANT
    vt = pythoncom.VT_ARRAY | pythoncom.VT_VARIANT
    return v(vt, [v(vt, tuple(r)) for r in rows])


class LabVIEWError(RuntimeError):
    pass


def _check(err, what):
    if isinstance(err, tuple) and len(err) == 3 and err[0]:
        raise LabVIEWError(f'{what}: error {err[1]}: {err[2]}')


class Session:
    def __init__(self):
        self.app = win32com.client.dynamic.Dispatch('LabVIEW.Application')._oleobj_
        self._refs = {}           # path -> VI COM ref; holding them keeps edited VIs in memory
        self._workers = {}        # (kind, cls, member, write) -> (path, inputs, outputs)
        WORKERS.mkdir(parents=True, exist_ok=True)
        self._trav = self.vi(LV / 'vi.lib' / 'Utility' / 'traverseref.llb' / 'Traverse for GObjects.vi')
        self._rr = self.vi(LV / 'vi.lib' / 'Utility' / 'QuickDropSupport' / 'Remove and Rewire Objects_core.vi')
        self._ctrl_d = self.vi(LV / 'vi.lib' / 'Utility' / 'QuickDropSupport' / 'Wire All Terminals_core.vi')
        self._cin = self.vi(LVS / 'Create Invoke Node.vi')
        self._cpn = self.vi(LVS / 'Create Property Node.vi')

    # ---- COM plumbing -------------------------------------------------
    def vi(self, path, fresh=False):
        path = str(Path(path).resolve())
        if fresh or path not in self._refs:
            self._refs[path] = self.app.Invoke(DISPID_GET_VI_REFERENCE, 0, METHOD, True, path, '', False, 0)
        return self._refs[path]

    def _invoke(self, ref, name, *args):
        return ref.Invoke(ref.GetIDsOfNames(name), 0, METHOD, True, *args)

    def call(self, ref, params):
        names, values = ref.InvokeTypes(DISPID_CALL, 0, METHOD, (24, 0), ((16396, 3), (16396, 3)),
                                        list(params), list(params.values()))
        return dict(zip(names, values))

    def save(self, path, as_path=None):
        self._invoke(self.vi(path), 'SaveInstrument', str(Path(as_path or path).resolve()), False, False)

    def revert(self, path):
        self._invoke(self.vi(path), 'Revert')

    def render(self, path, out_dir):
        """Export front panel (<name>p.png) and block diagram (<name>d.png) images."""
        out_dir = Path(out_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(path).stem
        self._invoke(self.vi(path), 'PrintVIToHTML', str(out_dir / f'{stem}.html'), False, 4, 0, 24, str(out_dir))
        return {k: out_dir / f'{stem}{k[0]}.png' for k in ('panel', 'diagram')
                if (out_dir / f'{stem}{k[0]}.png').exists()}

    # ---- object discovery / deletion ---------------------------------
    def find(self, vi_path, cls, target=1):
        """All GObject refs of VI Server class `cls` (e.g. TopLevelDiagram, Invoke, Terminal) on BD(1)/FP(0)."""
        out = self.call(self._trav, {'VI Refnum': self.vi(vi_path), 'Class Name': cls, 'Traverse Target': target,
                                     'Traverse Generated Code (F)': False, 'error in (no error)': NO_ERR,
                                     'References': [], '# of Refs': 0, 'error out': NO_ERR, 'dup VI Refnum': 0})
        _check(out['error out'], f'traverse {cls}')
        refreshed = self.vi(vi_path, fresh=True)   # Traverse invalidates the VI ref it was given
        if Path(vi_path).resolve() == (LVS / 'Create Invoke Node.vi').resolve():
            self._cin = refreshed
        if Path(vi_path).resolve() == (LVS / 'Create Property Node.vi').resolve():
            self._cpn = refreshed
        return list(out['References'])

    def diagram(self, vi_path):
        return self.find(vi_path, 'TopLevelDiagram')[0]

    def remove(self, items, clean_wires=True):
        out = self.call(self._rr, {'Items to Remove': list(items), 'Clean Wires (T)': clean_wires,
                                   'error in (no error)': NO_ERR, 'Any Items Removed?': False, 'error out': NO_ERR})
        _check(out['error out'], 'remove')
        return out['Any Items Removed?']

    # ---- VI creation --------------------------------------------------
    def _ensure_blank(self):
        if BLANK.exists():
            return
        shutil.copyfile(LV / 'templates' / 'Frameworks' / 'SubVI.vit', BLANK)
        self.remove(self.find(BLANK, 'Structure'))
        while self.remove(self.find(BLANK, 'Terminal')[-1:], clean_wires=True):
            pass
        for wire in self.find(BLANK, 'Wire'):
            self.remove([wire])
        self.save(BLANK)

    def new_vi(self, path):
        """Create an empty VI at `path` (overwrites)."""
        self._ensure_blank()
        path = Path(path).resolve()
        self._refs.pop(str(path), None)
        shutil.copyfile(BLANK, path)
        self.vi(path)
        self.revert(path)  # LabVIEW may still hold the previous VI at this path in memory.
        return path

    # ---- workers ------------------------------------------------------
    def labels(self, vi_path):
        """Front-panel control labels, in panel order, via ExportVIStrings."""
        f = Path(tempfile.mkdtemp()) / 'strings.txt'
        self._invoke(self.vi(vi_path), 'ExportVIStrings', str(f), False, '', False, False)
        text = f.read_text(encoding='latin-1', errors='replace')
        return re.findall(r'^	<CONTROL ID=\d+ type="[^"]*" name="([^"]*)"', text, re.M)

    def panel(self, vi_path):
        """Top-level front-panel objects of a VI as {label: (block-diagram terminal ref, is_indicator)}."""
        out = {}
        for ctl in self.find(vi_path, 'Control', target=0):
            try:
                term = self.prop_get('Control', 'Terminal', ctl)   # fails for cluster elements
                label = self.prop_get('Text', 'Text', self.prop_get('Control', 'Label', ctl))
                out.setdefault(label, (term, bool(self.prop_get('Control', 'Indicator', ctl))))
            except LabVIEWError:
                pass   # cluster elements and decorations
        return out

    def _variant(self, base, wired, path):
        """Copy worker `base` to `path` keeping only the `wired` optional inputs connected."""
        if not path.exists():
            tmp = WORKERS / f'_tmp_{path.name}'
            self._refs.pop(str(tmp.resolve()), None)
            shutil.copyfile(base, tmp)
            keep = {'reference', 'error in (no error)', *wired}
            drop = [term for label, (term, ind) in self.panel(tmp).items() if not ind and label not in keep]
            if drop:
                self.remove(drop)
                self.method('VI', 'BD.Remove Bad Wires', self.vi(tmp))
            self.save(tmp, path)
            self._refs.pop(str(tmp.resolve()), None)
        return path, self.labels(path)

    def _object_worker(self, cls, wired):
        """Keep the primitive's native class constant and U32 style ring intact."""
        key = ('new-object', cls, frozenset(wired))
        if key in self._workers:
            return self._workers[key]
        source = LVS / 'Create Invoke Node.vi'
        ring = self.find(source, 'RingConstant')[0]
        styles = {name: int(value) for name, value in
                  self.prop_get('RingConstant', 'Strings And Values[]', ring)}
        tag = re.sub(r'[^A-Za-z0-9]+', '_', cls + '_' + '_'.join(sorted(wired)))
        path = WORKERS / f'function_New_VI_Object_native_{tag}.vi'
        if not path.exists():
            tmp = WORKERS / f'_tmp_{path.name}'
            self._refs.pop(str(tmp.resolve()), None)
            shutil.copyfile(source, tmp)
            self._invoke(self.vi(tmp), 'DisconnectFromLibrary')
            for kind in ('Structure', 'Property', 'Invoke', 'BuildArray'):
                self.remove(self.find(tmp, kind))
            self.remove([node for node in self.find(tmp, 'Function')
                         if self.prop_get('Node', 'Style', node) != 'New VI Object'])
            while self.remove(self.find(tmp, 'Terminal')[-1:]):
                pass
            self.method('VI', 'BD.Remove Bad Wires', self.vi(tmp))
            constant = self.find(tmp, 'ClassSpecifierConstant')[0]
            types = self.prop_get('ClassSpecifierConstant', 'AllTypes[]', constant)
            type_id = next((t[0] for t in types if t[2] == cls), None)
            if type_id is None:
                raise LabVIEWError(f'Unknown VI object class: {cls}')
            self.method('ClassSpecifierConstant', 'SetType', constant, **{'ID String': type_id})
            ring = self.find(tmp, 'RingConstant')[0]
            control = self.method('Constant', 'Change to Control', ring)['Change to Control']
            self.prop_set('Text', 'Text', self.prop_get('Control', 'Label', control), 'style')
            node = self.find(tmp, 'Function')[0]
            out = self.call(self._ctrl_d, {'SelList[]': [node], 'Create constants?': False,
                                         'User string': '', 'error in (no error)': NO_ERR,
                                         'changes made?': False, 'error out': NO_ERR})
            _check(out['error out'], 'wire New VI Object')
            terminals = self._terminals(node)
            for name in ('path', 'bounds', 'position/next to', 'style'):
                if name not in wired:
                    wire = self.prop_get('Terminal', 'Connected Wire', terminals[name])
                    if wire:
                        self.method('Generic', 'Delete', wire)
            self.method('VI', 'BD.Remove Bad Wires', self.vi(tmp))
            self.save(tmp, path)
            self._refs.pop(str(tmp.resolve()), None)
        # Traversing the creator invalidates its earlier COM reference.
        self._cin = self.vi(source, fresh=True)
        self._workers[key] = path, styles
        return self._workers[key]

    def new_object(self, owner, style=None, location=None, bounds=None, path=None, vi_object_class='Generic'):
        """Place a native style name/ID, or a subVI/custom control from path.

        Class is a VI Server class name. Styles come from LabVIEW's native U32
        ring (Formula Node=2004, For Loop=2002, Add=1050), not Node.Style strings
        passed as Variants. Coordinates default to (0, 0); bounds are (width,
        height). Omitted path/bounds are physically unwired. A wired path selects
        file loading and takes precedence over style.
        """
        inputs = {'Diagram in': owner, 'location (0, 0)': (0, 0) if location is None else location}
        wired = {'position/next to'}
        for terminal, label, value in (('position/next to', 'location (0, 0)', location),
                                       ('bounds', 'bounds', bounds), ('path', 'path', path)):
            if value is not None:
                wired.add(terminal)
                inputs[label] = str(Path(value).resolve()) if terminal == 'path' else value
        if path is None:
            wired.add('style')
        worker, styles = self._object_worker(vi_object_class, wired)
        if path is None:
            if isinstance(style, str):
                if style not in styles:
                    raise LabVIEWError(f'Unknown native VI object style: {style}')
                style = styles[style]
            if style is None:
                raise LabVIEWError('Specify a native style or a subVI/control path')
            inputs['style'] = win32com.client.VARIANT(pythoncom.VT_UI4, style)
        out = self.run(worker, inputs, ['error out', 'object refnum'])
        _check(out['error out'], f'New VI Object {path or style}')
        return out['object refnum']

    def worker(self, kind, cls, member, write=False, wired=None):
        """Generate (or reuse) a worker VI for an invoke method or property of class `cls`.
        kind: 'method' | 'property'. cls: VI Server class short name, e.g. 'GObject', 'Terminal'.
        wired: input names to keep connected; other optional inputs are deleted so LabVIEW sees
        them as unwired (some methods, e.g. GObject.Replace, accept only one of several inputs)."""
        key = (kind, cls, member, write, wired)
        if key in self._workers:
            return self._workers[key]
        base, names = self.worker(kind, cls, member, write) if wired is not None else (None, None)
        tag = '' if wired is None else '__' + '_'.join(sorted(wired))
        safe = re.sub(r'[^A-Za-z0-9]+', '_', f'{kind}_{cls}_{member}_{"w" if write else "r"}{tag}')
        path = WORKERS / f'{safe}.vi'
        if base is not None:
            self._workers[key] = self._variant(base, wired, path)
            return self._workers[key]
        if not path.exists():
            tmp = WORKERS / f'_tmp_{safe}.vi'
            self.new_vi(tmp)
            top = self.diagram(tmp)
            common = {'Diagram in': top, 'Class Name': f'VI Server:{cls}', 'location (0, 0)': (200, 120),
                      'reference': 0, 'error in': 0, 'Inputs': [], 'error in (no error)': NO_ERR,
                      'Diagram out': 0, 'Outputs': [], 'error out': NO_ERR}
            if kind == 'method':
                out = self.call(self._cin, {**common, 'ID String': member})
            else:
                out = self.call(self._cpn, {**common, 'Properties': cluster_array([(member, write)])})
            _check(out['error out'], f'create {kind} {cls}.{member}')
            node = self.find(tmp, 'Invoke' if kind == 'method' else 'Property')
            out = self.call(self._ctrl_d, {'SelList[]': node, 'Create constants?': False, 'User string': '',
                                           'error in (no error)': NO_ERR, 'changes made?': False, 'error out': NO_ERR})
            _check(out['error out'], 'wire all terminals')
            self.save(tmp, path)   # save-as: only a fully built worker ever appears under its real name
            self._refs.pop(str(tmp.resolve()), None)
        names = self.labels(path)
        self._workers[key] = path, names
        return self._workers[key]

    def _terminals(self, node):
        return {self.prop_get('Terminal', 'Name', t): t for t in self.prop_get('Node', 'Terminals[]', node)}

    def chain_worker(self, steps):
        """Worker for a chain of members where each step's reference is the previous step's
        result, e.g. [('property', 'VI', 'ConPane.Ref', False), ('method', 'ConnectorPane',
        'AssignCtrlToTerm', False)]. Intermediate refs (that may not cross COM) stay inside LabVIEW."""
        safe = re.sub(r'[^A-Za-z0-9]+', '_', 'chain__' + '__'.join(f'{k}_{c}_{m}_{"w" if w else "r"}'
                                                                  for k, c, m, w in steps))
        path = WORKERS / f'{safe}.vi'
        if path in self._workers:
            return self._workers[path]
        if not path.exists():
            tmp = WORKERS / f'_tmp_{safe}.vi'
            self.new_vi(tmp)
            top = self.diagram(tmp)
            nodes = []
            for i, (kind, cls, member, write) in enumerate(steps):
                loc = (100 + 300 * i, 120)
                common = {'Diagram in': top, 'Class Name': f'VI Server:{cls}', 'location (0, 0)': loc,
                          'reference': 0, 'error in': 0, 'Inputs': [], 'error in (no error)': NO_ERR,
                          'Diagram out': 0, 'Outputs': [], 'error out': NO_ERR}
                if kind == 'method':
                    out = self.call(self._cin, {**common, 'ID String': member})
                else:
                    out = self.call(self._cpn, {**common, 'Properties': cluster_array([(member, write)])})
                _check(out['error out'], f'create {kind} {cls}.{member}')
                cands = self.find(tmp, 'Invoke' if kind == 'method' else 'Property')
                nodes.append(min(cands, key=lambda n: sum(abs(a - b) for a, b in zip(self.prop_get('GObject', 'Position', n), loc))))
            for (_, _, member, _), a, b in zip(steps, nodes, nodes[1:]):
                ta, tb = self._terminals(a), self._terminals(b)
                src = ta.get(member) or next(t for n, t in ta.items() if n not in ('reference', 'reference out',
                                             'error in (no error)', 'error out') and self.prop_get('Terminal', 'Is Source?', t))
                self.method('Terminal', 'Connect Wire', tb['reference'], **{'Wire Source': src})
                self.method('Terminal', 'Connect Wire', tb['error in (no error)'], **{'Wire Source': ta['error out']})
            out = self.call(self._ctrl_d, {'SelList[]': nodes, 'Create constants?': False, 'User string': '',
                                           'error in (no error)': NO_ERR, 'changes made?': False, 'error out': NO_ERR})
            _check(out['error out'], 'wire all terminals')
            self.save(tmp, path)
            self._refs.pop(str(tmp.resolve()), None)
        self._workers[path] = path, self.labels(path)
        return self._workers[path]

    def chain(self, steps, ref, **inputs):
        path, names = self.chain_worker(steps)
        inputs = {'reference': ref, **inputs}
        outs = [n for n in names if n not in inputs and n != 'error in (no error)' and not n.startswith('reference out')]
        out = self.run(path, inputs, outs)
        _check(out.pop('error out', None), 'chain ' + ' -> '.join(m for _, _, m, _ in steps))
        return out

    def run(self, path, inputs, outputs):
        ref = self.vi(path)
        self._invoke(ref, 'ReinitializeAllToDefault')
        for name, value in inputs.items():
            self._invoke(ref, 'SetControlValue', name, value)
        self._invoke(ref, 'Run', False)
        return {name: self._invoke(ref, 'GetControlValue', name) for name in outputs}

    def _member(self, kind, cls, member, ref, write, inputs):
        wired = frozenset(inputs) if kind == 'method' else None
        path, names = self.worker(kind, cls, member, write, wired)
        inputs = {'reference': ref, **inputs}
        unknown = set(inputs) - set(names)
        if unknown:
            raise LabVIEWError(f'{cls}.{member}: no parameter {sorted(unknown)}; has {names}')
        outs = [n for n in names if n not in inputs and n != 'error in (no error)' and not n.startswith('reference out')]
        out = self.run(path, inputs, outs)
        _check(out.pop('error out', None), f'{cls}.{member}')
        return out

    def method(self, cls, member, ref, **inputs):
        """Invoke scripting method `cls.member` on GObject ref; returns its outputs by name."""
        return self._member('method', cls, member, ref, False, inputs)

    def prop_get(self, cls, member, ref):
        out = self._member('property', cls, member, ref, False, {})
        return out[member] if member in out else next(iter(out.values()))

    def prop_set(self, cls, member, ref, value):
        path, names = self.worker('property', cls, member, True)
        label = member if member in names else next(n for n in names if n not in
                                                    ('reference', 'error in (no error)', 'error out') and not n.startswith('reference out'))
        self._member('property', cls, member, ref, True, {label: value})
