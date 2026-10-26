"""Backend-only deployment gate: python -m pitwall.predeploy [--live]."""
import argparse
import json
import unittest
from .data import ROOT
from .validate import matrix


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',help='Also evaluate every scenario with configured Gemini (uses quota)')
    args=parser.parse_args()
    folder=ROOT/'.local/validation';folder.mkdir(parents=True,exist_ok=True)
    suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'))
    tests=unittest.TextTestRunner(verbosity=1).run(suite)
    summary={'unit_tests':tests.testsRun,'unit_failures':len(tests.failures),'unit_errors':len(tests.errors),'status':'fail'}
    path=folder/'predeploy-summary.json'
    if not tests.wasSuccessful():
        path.write_text(json.dumps(summary,indent=2));raise SystemExit(1)
    local=matrix(output=folder/'local-scenarios.json')
    summary['local']={k:local[k] for k in ('passed','failed','not_run','verdict')}
    passed=local['verdict']=='pass'
    if args.live:
        live=matrix(mode='llm',output=folder/'llm-scenarios.json')
        summary['live']={k:live[k] for k in ('passed','failed','not_run','verdict')}
        passed=passed and live['verdict']=='pass'
    else:summary['live']='Not run by this invocation; use --live to include real Gemini.'
    summary['status']='pass' if passed else 'fail'
    path.write_text(json.dumps(summary,indent=2))
    print('Backend deployment gate: '+summary['status'].upper()+'. '+str(path),flush=True)
    raise SystemExit(0 if passed else 1)


if __name__=='__main__':main()
