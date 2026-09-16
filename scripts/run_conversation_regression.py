"""Run/resume a frozen regression suite; one idempotent request per turn."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor,as_completed
import json
import os
from pathlib import Path
import subprocess
from threading import Event, Lock
import urllib.request
from uuid import uuid4


class Client:
    def __init__(self,args):
        self.args=args
        self.token=json.loads(args.access_file.read_text(encoding='utf-8'))['token']
        self.env=dict(os.environ)
        if args.vercel_cli:
            self.env['VERCEL_TOKEN']=json.loads((args.vercel_auth_dir/'auth.json').read_text(encoding='utf-8'))['token']

    def request(self,path,payload=None):
        body=json.dumps(payload,ensure_ascii=False) if payload is not None else None
        if self.args.vercel_cli:
            command=['node',str(self.args.vercel_cli),'curl',path,'--deployment',self.args.base_url,'--',
                     '-sS','--fail-with-body','-X','POST' if body else 'GET',
                     '-H','Content-Type: application/json','-H','Authorization: Bearer '+self.token]
            if body is not None: command+=['--data-binary','@-']
            result=subprocess.run(command,input=body,env=self.env,capture_output=True,text=True,encoding='utf-8',timeout=600)
            if result.returncode: raise RuntimeError('regression_http_request_failed:' + result.stdout[-1500:])
            return json.loads(result.stdout)
        request=urllib.request.Request(self.args.base_url.rstrip('/')+path,data=body.encode() if body else None,
            headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=600) as response: return json.load(response)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',required=True)
    parser.add_argument('--base-url',required=True)
    parser.add_argument('--access-file',type=Path,required=True)
    parser.add_argument('--vercel-cli',type=Path)
    parser.add_argument('--vercel-auth-dir',type=Path)
    parser.add_argument('--suite',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--split',choices=['development','validation','all'],default='development')
    parser.add_argument('--scenario',action='append')
    parser.add_argument('--workers',type=int,choices=range(1,9),default=1)
    parser.add_argument('--repetitions',type=int,choices=[1,2,3],default=1)
    parser.add_argument('--max-turns',type=int,default=24,
                        help='Maximum remote turns in this invocation; 0 means unlimited')
    args=parser.parse_args()
    if args.vercel_cli and not args.vercel_auth_dir: parser.error('--vercel-auth-dir is required')
    client=Client(args)
    specification=json.loads(args.suite.read_text(encoding='utf-8'))
    suite=client.request('/api/admin/regression/suites',{'workspace_id':args.workspace,'specification':specification})
    cases=[s for s in specification['scenarios'] if (args.split=='all' or s['split']==args.split)
           and (not args.scenario or s['key'] in args.scenario)]
    if not cases: parser.error('no matching scenarios')
    args.output.mkdir(parents=True,exist_ok=True)
    path=args.output/'manifest.json'
    if path.exists():
        manifest=json.loads(path.read_text(encoding='utf-8'))
        if manifest['suite_fingerprint']!=suite['fingerprint']: parser.error('output belongs to another suite')
        if manifest['base_url']!=args.base_url: parser.error('output belongs to another deployment')
        if {r['scenario'] for r in manifest['runs']} != {s['key'] for s in cases}:
            parser.error('resume with the original scenario selection')
    else:
        manifest={'suite_id':suite['id'],'suite_fingerprint':suite['fingerprint'],'base_url':args.base_url,
            'runs':[{'scenario':s['key'],'run_id':str(uuid4()),'repetition':r,'status':'pending'}
                    for s in cases for r in range(args.repetitions)]}
    mutex=Lock()
    provider_blocked=Event()
    remaining_turns=max(0,args.max_turns)
    def save():
        path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    def run(item):
        nonlocal remaining_turns
        if provider_blocked.is_set():
            return
        scenario=next(s for s in cases if s['key']==item['scenario'])
        report_path=args.output/(item['run_id']+'.json')
        try:
            start=0
            if item['status']!='pending':
                report=client.request(f"/api/admin/regression/{args.workspace}/runs/{item['run_id']}")
                report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                start=report['next_step']
                if report.get('active_step') is not None: raise RuntimeError('turn_still_running')
                if any(t.get('grade',{}).get('provider_limit') for t in report.get('turns',[])):
                    provider_blocked.set()
                    raise RuntimeError('provider_limit_invalidated_sequence_use_new_output_after_recovery')
            for step in range(start,len(scenario['steps'])):
                if provider_blocked.is_set():
                    return
                with mutex:
                    if args.max_turns and remaining_turns <= 0:
                        if item['status'] != 'pending': item.update(status='paused_budget',step=step)
                        save()
                        return
                    if args.max_turns: remaining_turns -= 1
                    item.update(status='running',step=step);save()
                report=client.request('/api/admin/regression/turn',{'workspace_id':args.workspace,
                    'suite_id':suite['id'],'scenario_key':scenario['key'],'run_id':item['run_id'],'step_index':step})
                report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps({'scenario':scenario['key'],'repetition':item['repetition'],'step':step,
                                  'outcome':report['turns'][step]['grade']['outcome']}),flush=True)
                grade=report['turns'][step]['grade']
                if grade.get('provider_limit'):
                    provider_blocked.set()
                    with mutex:
                        item.update(status='blocked_provider',outcome='inconclusive',provider_limit=grade['provider_limit']);save()
                    return
            outcomes={t['grade']['outcome'] for t in report['turns']}
            outcome='passed' if outcomes=={'passed'} else ('inconclusive' if 'inconclusive' in outcomes else 'failed')
            with mutex:
                item.update(status=report['status'],outcome=outcome);save()
        except Exception as exc:
            with mutex:
                item.update(status='request_error',outcome='inconclusive',error=type(exc).__name__,detail=str(exc));save()
            print(json.dumps({'scenario':scenario['key'],'error':type(exc).__name__}),flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures=[executor.submit(run,item) for item in manifest['runs'] if item['status']!='completed']
        for future in as_completed(futures): future.result()
    return 0 if all(r.get('outcome')=='passed' for r in manifest['runs']) else 1


if __name__=='__main__': raise SystemExit(main())
