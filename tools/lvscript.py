"""Native LabVIEW block-diagram scripting over COM (no DLL injection); 2021 and 2018 verified.

Every VI Server scripting member (invoke method / property) is reached through a
generated "worker" VI: a blank VI holding one configured Invoke/Property Node whose
terminals were auto-wired to controls by Quick Drop's Ctrl+D core. Workers are cached
under work/workers and driven through COM by control name.

GObject refnums (diagrams, nodes, terminals) cross COM as plain ints and stay valid
for the lifetime of the LabVIEW session.
"""
import difflib
import hashlib
import struct
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import pythoncom
import win32api
import win32com.client

LV = None  # resolved below from installation evidence, never through COM
ROOT = Path(__file__).resolve().parent.parent
SCRIPTING = ROOT / 'work' / 'lvscript'    # per-version creators derived from Erdos Miller LV-Scripting
LV18 = SCRIPTING / '18'                    # helpers replacing 2021-only vi.lib behaviour
WORKER_LABELS = {'引用': 'reference', '引用输出': 'reference out',
                 '错误输入（无错误）': 'error in (no error)', '错误输出': 'error out',
                 '位置': 'Position', '名称': 'Name', '连线源': 'Wire Source',
                 '自动布线? (F)': 'Auto Route? (F)', '自动查找路径？(F)': 'Auto Route? (F)',
                 '自动连线？(T)': 'Auto Wire? (T)', '连线规范': 'Wire Spec',
                 '待添加的块': 'Chunks To Add', '添加位置': 'Where To Add',
                 # New VI Object primitive terminals
                 '路径': 'path', '边界': 'bounds', '对象引用句柄': 'object refnum', '自动连线？(F)': 'auto wire? (F)'}
WORKERS = ROOT / 'work' / 'workers'
BLANK = WORKERS / '_blank.vi'
NO_ERR = (False, 0, '')
SUPPORTED = (18, 21)   # LabVIEW 2018 and 2021, the versions with a verified creator set

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


def _error_text(value):
    """Keep Unicode as supplied; preserve undecodable native bytes losslessly."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        # LabVIEW strings use the Windows ANSI code page, not necessarily UTF-8.
        try:
            return f'{raw.decode("mbcs", errors="strict")} [native bytes: {raw!r}]'
        except (UnicodeError, LookupError):
            return repr(raw)
    return str(value) if value is not None else ''


def format_com_error(error, operation):
    """One readable line from a pywin32 com_error: operation, HRESULT and LabVIEW's description."""
    parts = [operation]
    hresult = getattr(error, 'hresult', None)
    if isinstance(hresult, int):
        parts.append(f'HRESULT 0x{hresult & 0xFFFFFFFF:08X}')
    info = getattr(error, 'excepinfo', None) or ()
    description = info[2] if len(info) > 2 and info[2] else getattr(error, 'strerror', None)
    parts.append(_error_text(description) if description else _error_text(error))
    return ': '.join(parts)


def _installation_missing(home):
    required = (
        'LabVIEW.exe',
        'vi.lib/Utility/traverseref.llb',
        'vi.lib/Utility/QuickDropSupport/Remove and Rewire Objects_core.vi',
        'vi.lib/Utility/QuickDropSupport/Wire All Terminals_core.vi',
        'templates/Frameworks/SubVI.vit',
    )
    return [name for name in required if not (home / name).is_file()]


def _installation_identity(home):
    """Read executable identity without starting COM or trusting directory names."""
    home = Path(home).resolve()
    executable = home / 'LabVIEW.exe'
    version = win32api.GetFileVersionInfo(str(executable), '\\')
    major = version['FileVersionMS'] >> 16
    minor = version['FileVersionMS'] & 0xFFFF
    with executable.open('rb') as stream:
        stream.seek(0x3C)
        header = struct.unpack('<I', stream.read(4))[0]
        stream.seek(header)
        if stream.read(4) != b'PE\0\0':
            raise LabVIEWError(f'Invalid LabVIEW executable: {executable}')
        machine = struct.unpack('<H', stream.read(2))[0]
    if machine not in (0x14C, 0x8664):
        raise LabVIEWError(f'Unsupported LabVIEW architecture 0x{machine:X}: {executable}')
    return {'home': str(home), 'major': major, 'year': 2000 + major,
            'version': f'{major}.{minor}', 'bitness': 64 if machine == 0x8664 else 32}


def _key(label):
    """Compare worker labels loosely: 2021 shows 'ID String', 2018 resources say 'IDString'."""
    return re.sub(r'[^0-9a-z]', '', label.casefold())


def _words(label):
    return [w.casefold() for w in re.findall(r'[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+', label)]


def _abbreviates(short, long):
    """Word-by-word abbreviation (TermIdx ~ Terminal Index), never a near-miss typo (Positon ~ Position)."""
    def word(a, b):
        rest = iter(b)
        return a == b or (a[:1] == b[:1] and len(b) - len(a) >= 2 and all(c in rest for c in a))
    s, l = _words(short), _words(long)
    return len(s) == len(l) and all(map(word, s, l))


def _resolve(name, labels):
    """The label meant by `name`: same key, else the one label abbreviating it.

    2018 resources use data names (TermIdx, PropItems[]) where 2021 shows long names
    (Terminal Index, Property Items[]). Anything else is left for the caller to reject.
    """
    same = [label for label in labels if _key(label) == _key(name)]
    if same:
        return same[0]
    short = {_key(label): label for label in labels if _abbreviates(label, name)}
    return next(iter(short.values())) if len(short) == 1 else name


def _localized_names(home):
    """{English member: {localized label: English name}} from LabVIEW's own lvserver.rsc.

    In a localized LabVIEW each STR# resource lists (localized, English) pairs: the member
    name first, then its parameters. One localized word can map to several English names
    across members, so translations are only applied per member.
    """
    data = (home / 'resource' / 'lvserver.rsc').read_bytes()
    info, _, base, _ = struct.unpack('>4I', data[16:32])
    blocks = info + struct.unpack('>I', data[info + 44:info + 48])[0]
    names = {}
    for k in range(struct.unpack('>I', data[blocks:blocks + 4])[0] + 1):
        kind, count, offset = struct.unpack('>4sII', data[blocks + 4 + 12 * k:blocks + 16 + 12 * k])
        if kind != b'STR#':
            continue
        for j in range(count + 1):
            start = base + struct.unpack('>I', data[blocks + offset + 20 * j + 12:blocks + offset + 20 * j + 16])[0] + 4
            strings, p = [], start + 2
            for _ in range(struct.unpack('>H', data[start:start + 2])[0]):
                strings.append(data[p + 1:p + 1 + data[p]].decode('mbcs', errors='replace'))
                p += 1 + data[p]
            if len(strings) >= 2 and len(strings) % 2 == 0:
                pairs = names.setdefault(strings[1], {})
                for native, english in zip(strings[0::2], strings[1::2]):
                    pairs.setdefault(native, english)
    return names


def _requested_major():
    value = os.environ.get('LABVIEW_VERSION', '').strip()
    if not value:
        return None
    try:
        major = int(value.split('.')[0])
    except ValueError:
        raise LabVIEWError('LABVIEW_VERSION must be a year or major version, e.g. 2021 or 18.0') from None
    return major - 2000 if major >= 2000 else major


def _discover_labview():
    """Find a Development System using explicit override, registry, or disk evidence."""
    override = os.environ.get('LABVIEW_HOME')
    requested = _requested_major()
    if override:
        home = Path(os.path.expandvars(override)).expanduser().resolve()
        missing = _installation_missing(home)
        if missing:
            raise LabVIEWError(f'LABVIEW_HOME={home} is not a usable LabVIEW Development System; '
                               f'missing: {", ".join(missing)}. Point LABVIEW_HOME at its installation directory.')
        identity = _installation_identity(home)
        if requested is not None and identity['major'] != requested:
            raise LabVIEWError(f'LABVIEW_HOME targets {identity["year"]}, not LABVIEW_VERSION={os.environ["LABVIEW_VERSION"]}')
        return home
    candidates = []
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, r'SOFTWARE\National Instruments\LabVIEW',
                                        0, winreg.KEY_READ | view) as root:
                        for index in range(winreg.QueryInfoKey(root)[0]):
                            name = winreg.EnumKey(root, index)
                            if name != 'CurrentVersion' and not re.fullmatch(r'\d+(?:\.\d+)*', name):
                                continue
                            try:
                                with winreg.OpenKey(root, name) as key:
                                    value = winreg.QueryValueEx(key, 'Path')[0]
                                if isinstance(value, str) and value:
                                    candidates.append(Path(os.path.expandvars(value)))
                            except OSError:
                                continue
                except OSError:
                    continue
    except ImportError:
        pass
    for variable in ('ProgramW6432', 'ProgramFiles', 'ProgramFiles(x86)'):
        if os.environ.get(variable):
            base = Path(os.environ[variable]) / 'National Instruments'
            if base.is_dir():
                candidates.extend(sorted(base.glob('LabVIEW *'), reverse=True))
    rejected = []
    seen = set()
    usable = []
    for candidate in candidates:
        home = candidate.resolve()
        identity = str(home).casefold()
        if identity in seen:
            continue
        seen.add(identity)
        missing = _installation_missing(home)
        if missing:
            rejected.append(f'{home} (missing {", ".join(missing)})')
            continue
        major = _installation_identity(home)['major']
        if major not in SUPPORTED:
            rejected.append(f'{home} (LabVIEW {2000 + major} is not supported yet)')
        elif requested in (None, major):
            usable.append(home)
    if len(usable) == 1:
        return usable[0]
    if len(usable) > 1:
        raise LabVIEWError('Multiple LabVIEW installations match: ' + '; '.join(map(str, usable)) +
                           '. Set LABVIEW_HOME to an exact installation (or LABVIEW_VERSION to a unique year).')
    details = '; '.join(rejected) or 'no registered or installed Development System found'
    raise LabVIEWError('Cannot locate a matching usable LabVIEW Development System: '
                       f'{details}. Install the licensed Development System (not only the Run-Time), '
                       'then set LABVIEW_HOME to its installation directory. COM automation and '
                       'VI Scripting must be enabled in LabVIEW.')


try:
    LV = _discover_labview()
except LabVIEWError:
    # Importing the tool module must still work when LabVIEW is not installed.
    pass


def _check(err, what):
    if isinstance(err, (tuple, list)) and len(err) == 3 and err[0]:
        code = int(err[1])
        source = _error_text(err[2]).strip()
        raise LabVIEWError(f'{what}: LabVIEW error {code} (0x{code & 0xFFFFFFFF:08X}); '
                           f'source: {source or "<empty native error source>"}')


class Session:
    def __init__(self):
        global LV, WORKERS, BLANK
        LV = _discover_labview()
        identity = _installation_identity(LV)
        if identity['major'] not in SUPPORTED:
            raise LabVIEWError(f'Unsupported LabVIEW version {identity["version"]}; supported targets are 2018 and 2021')
        try:
            # Both installations share a CLSID. Select the registry architecture
            # explicitly, then verify the actual server before loading any VI.
            activation = 0x80000 if identity['bitness'] == 64 else 0x40000
            self.app = pythoncom.CoCreateInstance('{9A872070-0A06-11D1-90B7-00A024CE2744}', None,
                                                 pythoncom.CLSCTX_LOCAL_SERVER | activation,
                                                 pythoncom.IID_IDispatch)
            actual_home = Path(self.app.Invoke(self.app.GetIDsOfNames('ApplicationDirectory'), 0,
                                              pythoncom.DISPATCH_PROPERTYGET, True)).resolve()
            actual_version = self.app.Invoke(self.app.GetIDsOfNames('Version'), 0,
                                             pythoncom.DISPATCH_PROPERTYGET, True)
        except pythoncom.com_error as error:
            raise LabVIEWError(format_com_error(error, f'Connect LabVIEW {identity["year"]} {identity["bitness"]}-bit ({LV})')) from error
        if actual_home != LV or int(actual_version.split('.')[0]) != identity['major']:
            raise LabVIEWError(f'LabVIEW COM target mismatch: requested {LV} ({identity["version"]}), '
                               f'connected {actual_home} ({actual_version}). No VI was loaded or edited. '
                               'Register the intended LabVIEW COM server for this architecture before retrying.')
        tag = f'{identity["major"]}-{identity["bitness"]}-' + hashlib.sha256(f'{LV}|{ROOT}'.casefold().encode()).hexdigest()[:12]
        # Worker names carry the cache tag (LabVIEW home + this checkout), so VIs a LabVIEW session
        # still holds from an older cache version or another checkout never collide by name.
        WORKERS = ROOT / 'work' / 'workers' / (tag + '-v14')
        BLANK = WORKERS / '_blank.vi'
        WORKERS.mkdir(parents=True, exist_ok=True)
        self._creator_paths = {}
        for name in ('Create Invoke Node.vi', 'Create Property Node.vi'):
            target = WORKERS / f'{WORKERS.name}_{name}'
            if not target.exists():
                shutil.copyfile(SCRIPTING / str(identity['major']) / name, target)
            self._creator_paths[name] = target
        self._locale = json.loads((ROOT / 'tools' / 'native_locale_18.json').read_text(encoding='utf-8')) if identity['major'] == 18 else None
        self._names = _localized_names(LV) if identity['major'] == 18 else {}
        self._data_names = {_key(m): m for m in self._names}   # 'Set Properties[]' -> 'SetProperties[]'
        self._alias = {}          # worker path -> {native label: English name}
        self._labels = {}         # worker path -> [(native label, English name)], workers never change
        self.version_info = {**identity, 'actual_version': actual_version,
                             'cache': str(WORKERS)}
        self._refs = {}           # path -> VI COM ref; holding them keeps edited VIs in memory
        self._workers = {}        # (kind, cls, member, write) -> (path, inputs, outputs)
        self._styles = None       # Python values only; never cached GObject refs
        self._trav = self.vi(LV / 'vi.lib' / 'Utility' / 'traverseref.llb' / 'Traverse for GObjects.vi')
        self._rr = self.vi(LV / 'vi.lib' / 'Utility' / 'QuickDropSupport' / 'Remove and Rewire Objects_core.vi')
        self._ctrl_d = self.vi(LV / 'vi.lib' / 'Utility' / 'QuickDropSupport' / 'Wire All Terminals_core.vi')
        self._cin = self.vi(self._creator_paths['Create Invoke Node.vi'])
        self._cpn = self.vi(self._creator_paths['Create Property Node.vi'])

    # ---- COM plumbing -------------------------------------------------
    def vi(self, path, fresh=False):
        path = str(Path(path).resolve())
        if fresh or path not in self._refs:
            try:
                self._refs[path] = self.app.Invoke(DISPID_GET_VI_REFERENCE, 0, METHOD, True, path, '', False, 0)
            except pythoncom.com_error as error:
                hint = ' Path exceeds 259 characters; install to a shorter directory.' if len(path) > 259 else ''
                raise LabVIEWError(format_com_error(error, f'GetVIReference {path}') + hint) from error
        return self._refs[path]

    def _invoke(self, ref, name, *args):
        try:
            return ref.Invoke(ref.GetIDsOfNames(name), 0, METHOD, True, *args)
        except pythoncom.com_error as error:
            raise LabVIEWError(format_com_error(error, f'VI.{name} [{self._path_of(ref)}]')) from error

    def call(self, ref, params):
        if self._names:
            path = self._path_of(ref)
            try:
                native = self._native_labels(path)
            except LabVIEWError:   # password-protected helper: its labels cannot be read, call them as given
                self._labels[str(Path(path).resolve())] = []
                native = {}
            params = {native.get(_key(k), k): v for k, v in params.items()}
        try:
            names, values = ref.InvokeTypes(DISPID_CALL, 0, METHOD, (24, 0), ((16396, 3), (16396, 3)),
                                            list(params), list(params.values()))
        except pythoncom.com_error as error:
            raise LabVIEWError(format_com_error(error, f'VI.Call [{self._path_of(ref)}]')) from error
        return {WORKER_LABELS.get(n, n): v for n, v in zip(names, values)}

    def _native_labels(self, path):
        """English (2021) label -> this LabVIEW's front-panel label, for a helper or worker VI."""
        return {_key(english): native for native, english in self._label_pairs(path)}

    def _path_of(self, ref):
        return next((path for path, cached in self._refs.items() if cached is ref), '<uncached VI>')

    def save(self, path, as_path=None):
        self._invoke(self.vi(path), 'SaveInstrument', str(Path(as_path or path).resolve()), False, False)

    def revert(self, path):
        self._invoke(self.vi(path), 'Revert')

    def render(self, path, out_dir):
        """Export into a new directory so previous images cannot masquerade as fresh output.

        The native print routine controls clipping/layout; this does not rearrange,
        save, revert, or otherwise mutate the target VI to change its appearance.
        """
        out_dir = Path(out_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        out_dir = Path(tempfile.mkdtemp(prefix='export_', dir=out_dir))
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
        if Path(vi_path).resolve() == self._creator_paths['Create Invoke Node.vi'].resolve():
            self._cin = refreshed
        if Path(vi_path).resolve() == self._creator_paths['Create Property Node.vi'].resolve():
            self._cpn = refreshed
        return list(out['References'])

    def diagram(self, vi_path):
        return self.find(vi_path, 'TopLevelDiagram')[0]

    def remove(self, items, clean_wires=True):
        if not items:
            return False
        if self.version_info['major'] == 18:
            removed = False
            for item in items:
                err = self.run(LV18 / 'Delete_18.vi', {'reference': item}, ['error out'])['error out']
                if err[1] == 1093:   # not deletable on its own (a node's terminal); 2021's helper skips these too
                    continue
                _check(err, 'delete')
                removed = True
            return removed
        params = {'Items to Remove': list(items), 'error in (no error)': NO_ERR,
                  'Any Items Removed?': False, 'error out': NO_ERR}
        if self.version_info['major'] >= 21:
            params['Clean Wires (T)'] = clean_wires
        elif not clean_wires:
            raise LabVIEWError('LabVIEW 2018 Remove and Rewire helper does not expose Clean Wires; clean_wires=False is unsupported')
        out = self.call(self._rr, params)
        _check(out['error out'], 'remove')
        return out['Any Items Removed?']

    # ---- VI creation --------------------------------------------------
    def _ensure_blank(self):
        if BLANK.exists():
            return
        if self.version_info['major'] == 18:
            shutil.copyfile(LV18 / 'Empty_18.vi', BLANK)
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
        """English front-panel control labels of a worker, in panel order."""
        return [english for _, english in self._label_pairs(vi_path)]

    def _label_pairs(self, vi_path):
        """(native, English) label pairs via ExportVIStrings, cached: worker files never change."""
        path = str(Path(vi_path).resolve())
        if path not in self._labels:
            with tempfile.TemporaryDirectory() as directory:
                f = Path(directory) / 'strings.txt'
                self._invoke(self.vi(path), 'ExportVIStrings', str(f), False, '', False, False)
                text = f.read_bytes().decode('mbcs', errors='strict')
            alias = self._alias.get(path, {})
            self._labels[path] = [(n, WORKER_LABELS.get(n) or alias.get(n, n))
                                  for n in re.findall(r'^\t<CONTROL ID=\d+ type="[^"]*" name="([^"]*)"', text, re.M)]
        return self._labels[path]

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

    def _variant(self, base, wired, path, alias):
        """Copy worker `base` to `path` keeping only the `wired` optional inputs connected."""
        self._alias[str(path.resolve())] = alias
        if not path.exists():
            tmp = WORKERS / f'_tmp_{path.name}'
            self._refs.pop(str(tmp.resolve()), None)
            shutil.copyfile(base, tmp)
            keep = {_key(k) for k in ('reference', 'error in (no error)', *wired)}
            drop = [term for label, (term, ind) in self.panel(tmp).items()
                    if not ind and _key(WORKER_LABELS.get(label) or alias.get(label, label)) not in keep]
            if drop:
                self.remove(drop)
                self.method('VI', 'BD.Remove Bad Wires', self.vi(tmp))
            self.save(tmp, path)
            self._refs.pop(str(tmp.resolve()), None)
        return path, self.labels(path)

    def _native_styles(self):
        """Cache native ring values, never its session-sensitive object reference."""
        if self._styles is None:
            source = self._creator_paths['Create Invoke Node.vi']
            rings = self.find(source, 'RingConstant')
            if not rings:
                raise LabVIEWError(f'Native style RingConstant missing in {source}')
            # The patched creator holds the New VI Object primitive's native ring.
            if self.version_info['major'] == 18:
                rows = self.run(LV18 / 'Ring_Styles_18.vi',
                                {'reference': rings[0]}, ['Strings And Values []'])['Strings And Values []']
            else:
                rows = self.prop_get('RingConstant', 'Strings And Values[]', rings[0])
            self._styles = {str(name): int(value) for name, value in rows}
        return self._styles

    def search_styles(self, query='', limit=50, offset=0):
        """List native name/ID pairs; substring matches precede fuzzy candidates.

        Pagination is deterministic for the session's native creator ring. Results
        suggest names only: new_object never silently substitutes a candidate.
        """
        if limit < 1 or offset < 0:
            raise ValueError('limit must be >= 1 and offset >= 0')
        styles = self._native_styles()
        names = sorted(styles, key=lambda name: (name.casefold(), name))
        needle = query.strip().casefold()
        if needle:
            substring = [name for name in names if needle in name.casefold()]
            fuzzy = []
            for name in names:
                if needle in name.casefold():
                    continue
                score = difflib.SequenceMatcher(None, needle, name.casefold()).ratio()
                if score >= 0.6:
                    fuzzy.append((score, name))
            fuzzy.sort(key=lambda item: (-item[0], item[1].casefold(), item[1]))
            names = substring + [name for _, name in fuzzy]
        return {'total': len(names),
                'matches': [{'name': name, 'id': styles[name]} for name in names[offset:offset + limit]]}

    def _object_worker(self, cls, wired):
        """Keep the primitive's native class constant and U32 style ring intact."""
        key = ('new-object', cls, frozenset(wired))
        if key in self._workers:
            return self._workers[key]
        source = self._creator_paths['Create Invoke Node.vi']
        styles = self._native_styles()
        tag = re.sub(r'[^A-Za-z0-9]+', '_', cls + '_' + '_'.join(sorted(wired)))
        path = WORKERS / f'{WORKERS.name}_function_New_VI_Object_native_{tag}.vi'
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
        if self.version_info['major'] == 18 and path is None and vi_object_class == 'Generic':
            worker = LV18 / ('New_Object_Bounds_18.vi' if bounds is not None else 'New_Object_18.vi')
            styles = self._native_styles()
        else:
            worker, styles = self._object_worker(vi_object_class, wired)
        if path is None:
            if isinstance(style, str):
                if style not in styles:
                    candidates = self.search_styles(style, limit=8)['matches']
                    choices = ', '.join(f'{item["name"]!r} (ID {item["id"]})' for item in candidates)
                    raise LabVIEWError(f'Unknown native VI object style: {style!r}; '
                                       f'candidates: {choices or "none"}. Use search_styles to list native names; '
                                       'select an exact name or numeric ID explicitly.')
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
        path = WORKERS / f'{WORKERS.name}_{safe}.vi'
        alias = self._member_names(member)
        if base is not None:
            self._workers[key] = self._variant(base, wired, path, alias)
            return self._workers[key]
        self._alias[str(path.resolve())] = alias
        if not path.exists():
            tmp = WORKERS / f'_tmp_{path.name}'
            self.new_vi(tmp)
            self._member_node(self.diagram(tmp), kind, cls, member, write, (200, 120))
            node = self.find(tmp, 'Invoke' if kind == 'method' else 'Property')
            out = self.call(self._ctrl_d, {'SelList[]': node, 'Create constants?': False, 'User string': '',
                                           'error in (no error)': NO_ERR, 'changes made?': False, 'error out': NO_ERR})
            _check(out['error out'], 'wire all terminals')
            self.save(tmp, path)   # save-as: only a fully built worker ever appears under its real name
            self._refs.pop(str(tmp.resolve()), None)
        names = self.labels(path)
        self._workers[key] = path, names
        return self._workers[key]

    def _member_node(self, diagram, kind, cls, member, write, location):
        """Drop one Invoke/Property Node for `cls.member`; localized LabVIEW wants native class/property names."""
        native_class = self._locale['classes'].get(cls, cls) if self._locale else cls
        common = {'Diagram in': diagram, 'Class Name': f'VI Server:{native_class}', 'location (0, 0)': location,
                  'reference': 0, 'error in': 0, 'Inputs': [], 'error in (no error)': NO_ERR,
                  'Diagram out': 0, 'Outputs': [], 'error out': NO_ERR}
        if kind == 'method':
            out = self.call(self._cin, {**common, 'ID String': self._data_names.get(_key(member), member)})
        else:
            out = self.call(self._cpn, {**common, 'Properties': cluster_array([(self._property_id(cls, member), write)])})
        _check(out['error out'], f'create {kind} {cls}.{member}')

    def _property_id(self, cls, member):
        """Localized LabVIEW selects properties by UID; 2021 long names map to 2018 data names."""
        props = self._locale['properties'].get(cls) if self._locale else None
        if props is None:
            return member
        name = _resolve(member, props)
        if name not in props:
            close = difflib.get_close_matches(member, list(props), n=5, cutoff=0.4)
            raise LabVIEWError(f'{cls}.{member}: no such property in LabVIEW {self.version_info["year"]}; '
                               f'close names: {close}')
        return props[name]

    def _member_names(self, member):
        """{localized label: English name} for one member of a localized LabVIEW, else {}."""
        return self._names.get(self._data_names.get(_key(member), member), {})

    def _terminals(self, node):
        return {self.prop_get('Terminal', 'Name', t): t for t in self.prop_get('Node', 'Terminals[]', node)}

    def chain_worker(self, steps):
        """Worker for a chain of members where each step's reference is the previous step's
        result, e.g. [('property', 'VI', 'ConPane.Ref', False), ('method', 'ConnectorPane',
        'AssignCtrlToTerm', False)]. Intermediate refs (that may not cross COM) stay inside LabVIEW."""
        safe = re.sub(r'[^A-Za-z0-9]+', '_', 'chain__' + '__'.join(f'{k}_{c}_{m}_{"w" if w else "r"}'
                                                                  for k, c, m, w in steps))
        path = WORKERS / f'{WORKERS.name}_{safe}.vi'
        if path in self._workers:
            return self._workers[path]
        self._alias[str(path.resolve())] = {n: e for _, _, m, _ in steps for n, e in self._member_names(m).items()}
        if not path.exists():
            tmp = WORKERS / f'_tmp_{path.name}'
            self.new_vi(tmp)
            top = self.diagram(tmp)
            nodes = []
            for i, (kind, cls, member, write) in enumerate(steps):
                loc = (100 + 300 * i, 120)
                self._member_node(top, kind, cls, member, write, loc)
                cands = self.find(tmp, 'Invoke' if kind == 'method' else 'Property')
                nodes.append(min(cands, key=lambda n: sum(abs(a - b) for a, b in zip(self.prop_get('GObject', 'Position', n), loc))))
            alias = self._alias[str(path.resolve())]
            for (_, _, member, _), a, b in zip(steps, nodes, nodes[1:]):
                ta, tb = ({WORKER_LABELS.get(n) or alias.get(n, n): t for n, t in self._terminals(x).items()} for x in (a, b))
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
        inputs = {'reference': ref, **{_resolve(k, names): v for k, v in inputs.items()}}
        outs = [n for n in names if n not in inputs and n != 'error in (no error)' and not n.startswith('reference out')]
        out = self.run(path, inputs, outs)
        _check(out.pop('error out', None), 'chain ' + ' -> '.join(m for _, _, m, _ in steps))
        return out

    def run(self, path, inputs, outputs):
        ref = self.vi(path)
        # Translate only our own worker VIs; a user's VI is run with its labels exactly as given.
        native = self._native_labels(path) if self._names and Path(path).resolve().is_relative_to(WORKERS) else {}
        self._invoke(ref, 'ReinitializeAllToDefault')
        for name, value in inputs.items():
            self._invoke(ref, 'SetControlValue', native.get(_key(name), name), value)
        self._invoke(ref, 'Run', False)
        return {name: self._invoke(ref, 'GetControlValue', native.get(_key(name), name)) for name in outputs}

    def _member(self, kind, cls, member, ref, write, inputs):
        if kind == 'method' and inputs:
            full = self.worker(kind, cls, member, write)[1]
            inputs = {_resolve(k, full): v for k, v in inputs.items()}
        wired = frozenset(inputs) if kind == 'method' else None
        path, names = self.worker(kind, cls, member, write, wired)
        inputs = {'reference': ref, **inputs}
        known = {_key(n) for n in names}
        unknown = {k for k in inputs if _key(k) not in known}
        if unknown:
            # A wired variant only keeps the requested inputs; report the member's full list.
            full = self.worker(kind, cls, member, write)[1] if wired is not None else names
            params = [n for n in full if n not in ('reference', 'reference out', 'error in (no error)', 'error out')]
            hints = {u: difflib.get_close_matches(u, params, n=1) for u in sorted(unknown - {'reference'})}
            hint = '; '.join(f'{u!r} -> did you mean {m[0]!r}?' for u, m in hints.items() if m)
            raise LabVIEWError(f'{cls}.{member}: unknown parameter {sorted(unknown)}; '
                               f'native parameters: {params}' + (f'; {hint}' if hint else ''))
        given = {_key(k) for k in inputs}
        outs = [n for n in names if _key(n) not in given and n != 'error in (no error)' and not n.startswith('reference out')]
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
        if self._locale and isinstance(value, str) and value.startswith('VI Server:'):
            # e.g. Property.PropClassName: localized LabVIEW only accepts its own class names.
            value = 'VI Server:' + self._locale['classes'].get(value[10:], value[10:])
        path, names = self.worker('property', cls, member, True)
        label = member if member in names else next(n for n in names if n not in
                                                    ('reference', 'error in (no error)', 'error out') and not n.startswith('reference out'))
        self._member('property', cls, member, ref, True, {label: value})
