"""Generate editing workers for an explicitly selected LabVIEW installation; no user VI is edited."""
import argparse
import json
import os
import time

import pythoncom

from lvscript import Session, _resolve


READS = {
    'VI': ('Panel', 'Exec.State'),
    'GObject': ('Class Name', 'Owner', 'Position', 'Bounds'),
    'Node': ('Terminals[]', 'Label', 'Style'),
    'Text': ('Text',),
    'Constant': ('Value', 'Label', 'Terminal'),
    'Control': ('Label', 'Terminal', 'Indicator', 'Value', 'DefVal'),
    'Terminal': ('Name', 'Is Source?', 'Connected Wire'),
    'Wire': ('Terminals[]', 'Is Broken?'),
    'Tunnel': ('Outside Term', 'Inside Terminals[]'),
    'Loop': ('LoopCounter',),
    'ForLoop': ('LoopCount', 'HasCondTerm', 'LoopEndRef'),
    'WhileLoop': ('LoopEndRef',),
    'RightShiftRegister': ('Left Registers[]',),
}
WRITES = {
    'GObject': ('Position',),
    'Text': ('Text',),
    'Control': ('Value', 'DefVal'),
    'Constant': ('Value',),
    'Numeric': ('Rep',),
}
METHODS = (
    ('Generic', 'Delete', ()),
    ('VI', 'BD.Remove Bad Wires', ()),
    ('Terminal', 'Connect Wire', ('Wire Source', 'Auto Route? (F)')),
    ('GrowableFunction', 'Resize.AddChunk', ('Chunks To Add',)),
    ('Diagram', 'CleanUp', ()),
)


def warmup(session):
    """Return a JSON-safe report; failures remain visible and make the CLI exit nonzero."""
    started = time.perf_counter()
    before = session.worker_event_count
    prepared, failures = [], []

    def prepare(name, operation):
        tick = time.perf_counter()
        try:
            operation()
        except Exception as error:
            failures.append({'worker': name, 'error': str(error),
                             'action': 'Check the selected LabVIEW version, VI Scripting settings and native member/parameter names; rerun warmup after fixing the reported error.'})
        else:
            prepared.append({'worker': name, 'elapsed': time.perf_counter() - tick})

    for write, members in ((False, READS), (True, WRITES)):
        for cls, names in members.items():
            for member in names:
                prepare(f'property {cls}.{member} {"write" if write else "read"}',
                        lambda c=cls, m=member, w=write: session.worker('property', c, m, w))
    for cls, member, inputs in METHODS:
        def method(c=cls, m=member, names=inputs):
            _, labels = session.worker('method', c, m)
            wired = frozenset(_resolve(name, labels) for name in names)
            session.worker('method', c, m, wired=wired)
        prepare(f'method {cls}.{member} ({", ".join(inputs)})', method)
    # Read a backend-owned creator diagram to build bulk workers without opening
    # or modifying any user's VI. Traversal refreshes creator refs through find().
    for member in ('UID', 'Class Name', 'Left', 'Top'):
        prepare(f'bulk GObject.{member}',
                lambda m=member: session.prop_many('GObject', m,
                    [session.diagram(session._creator_paths['Create Invoke Node.vi'])]))

    # 2018's common Generic style placement uses shipped version-specific helpers.
    # Only generate the variants new_object actually uses on the selected version.
    for wired in ({'position/next to', 'style'}, {'position/next to', 'style', 'bounds'},
                  {'position/next to', 'path'}, {'position/next to', 'path', 'bounds'}):
        if session.version_info['major'] == 18 and 'style' in wired:
            continue
        prepare('object Generic ' + ','.join(sorted(wired)),
                lambda w=wired: session._object_worker('Generic', w))
    for tail in (('property', 'ConnectorPane', 'Pattern', True),
                 ('method', 'ConnectorPane', 'AssignCtrlToTerm', False)):
        steps = [('property', 'VI', 'ConPane.Ref', False), tail]
        prepare('chain VI.ConPane.Ref -> ConnectorPane.' + tail[2],
                lambda s=steps: session.chain_worker(s))
    return {'version': session.version_info, 'elapsed': time.perf_counter() - started,
            'prepared': prepared, 'failures': failures,
            'workers_created': session.worker_event_count - before,
            'worker_events': session.worker_events_since(before)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--labview-home', default=os.environ.get('LABVIEW_HOME'))
    parser.add_argument('--labview-version', default=os.environ.get('LABVIEW_VERSION'))
    args = parser.parse_args()
    if not args.labview_home or not args.labview_version:
        parser.error('Pin both LABVIEW_HOME and LABVIEW_VERSION, or pass --labview-home and --labview-version; warmup does not choose a different installation implicitly.')
    os.environ['LABVIEW_HOME'] = args.labview_home
    os.environ['LABVIEW_VERSION'] = args.labview_version
    pythoncom.CoInitialize()
    session = None
    try:
        session = Session()
        report = warmup(session)
        print(json.dumps(report, ensure_ascii=True, indent=2))
        if report['failures']:
            raise SystemExit(1)
        print('WARMUP_OK: editing and snapshot workers prepared for the pinned installation')
    finally:
        session = None
        pythoncom.CoUninitialize()


if __name__ == '__main__':
    main()
