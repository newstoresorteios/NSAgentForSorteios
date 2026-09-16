"""Import delivered conversations and evaluate the actual NSAgent remotely.

Credentials stay in local files/environment. The server reads the published
persona, settings and model credential. Every trial is stored independently.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--access-file', type=Path, required=True, help='Private JSON file containing token')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--case-id', action='append')
    parser.add_argument('--no-repair', action='store_true')
    parser.add_argument('--vercel-cli', type=Path, help='Optional path to Vercel vc.js for protected deployments')
    parser.add_argument('--vercel-auth-dir', type=Path)
    args = parser.parse_args()
    token = json.loads(args.access_file.read_text(encoding='utf-8'))['token']

    def request(path, payload):
        body = json.dumps(payload)
        if args.vercel_cli:
            if not args.vercel_auth_dir:
                parser.error('--vercel-auth-dir is required with --vercel-cli')
            auth = json.loads((args.vercel_auth_dir/'auth.json').read_text(encoding='utf-8'))
            # curl currently forwards global CLI flags; environment avoids that bug.
            env = {**os.environ, 'VERCEL_TOKEN': auth['token']}
            command = ['node',str(args.vercel_cli),'curl',path,'--deployment',args.base_url,'--',
                       '-sS','--fail-with-body','-X','POST','-H','Content-Type: application/json',
                       '-H','Authorization: Bearer '+token,'--data-binary',body]
            response = subprocess.run(command,env=env,capture_output=True,text=True,encoding='utf-8',timeout=600)
            if response.returncode:
                raise RuntimeError('remote_evaluation_request_failed')
            return json.loads(response.stdout)
        req = urllib.request.Request(args.base_url.rstrip('/')+path, data=body.encode(),
                                     headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        with urllib.request.urlopen(req, timeout=600) as response:
            return json.load(response)

    args.output.mkdir(parents=True, exist_ok=True)
    if args.case_id:
        cases = [{'id': value} for value in args.case_id]
    else:
        payload = {'workspace_id':args.workspace}
        if args.limit is not None:
            payload['limit'] = args.limit
        cases = request('/api/admin/evaluations/import',payload)['items']
    manifest = []
    for case in cases:
        run_id = str(uuid4())
        manifest.append({'case_id':case['id'],'request_id':run_id,'status':'requested'})
        (args.output/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
        try:
            report = request('/api/admin/evaluations/run', {'workspace_id':args.workspace,'case_id':case['id'],
                             'request_id':run_id,'repair':not args.no_repair})
        except Exception as exc:
            report = {'id':run_id,'status':'request_error','outcome':'inconclusive','error':type(exc).__name__}
        (args.output/(run_id+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        manifest[-1].update(status=report['status'],outcome=report.get('outcome'))
        (args.output/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
        print(json.dumps({'case':case['id'],'run':run_id,'status':report['status'],
                          'outcome':report.get('outcome'),'repair':(report.get('repair') or {}).get('status')}),flush=True)
        if report['status'] in {'error','request_error'}:
            return 2
    return 1 if any(item.get('outcome') != 'passed' for item in manifest) else 0


if __name__ == '__main__':
    raise SystemExit(main())
