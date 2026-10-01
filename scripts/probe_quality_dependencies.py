"""Read deployed health/diagnostics without model calls or customer messages."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
from dotenv import dotenv_values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--local-meta', action='store_true', help='Also verify the existing local Meta credential by GET only')
    args = parser.parse_args()
    values = dotenv_values(args.env_file)
    token = str(values.get('ADMIN_API_TOKEN') or '').strip()
    base = 'https://ns-agent-for-sorteios.vercel.app'
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'base': base,
              'model_calls': 0, 'customer_messages': 0}
    try:
        with httpx.Client(timeout=45, follow_redirects=False) as client:
            health = client.get(base + '/api/health')
            report['health_http_status'] = health.status_code
            if health.status_code == 200:
                report['health'] = {key: health.json().get(key) for key in (
                    'ok', 'deployment_sha', 'dry_run', 'environment', 'configuration_warning_count')}
            if token:
                response = client.get(base + '/api/admin/health', headers={'Authorization': 'Bearer ' + token})
                report['admin_http_status'] = response.status_code
                if response.status_code == 200:
                    body = response.json()
                    report['diagnostics'] = {key: body.get(key) for key in (
                        'meta_ig_graph', 'tray_adaptor_probe', 'configuration_warnings',
                        'agent_quality_judge_mode', 'agent_critique_mode', 'agent_max_llm_calls_per_turn')}
            else:
                report['admin_check'] = 'credential_unavailable'
    except Exception as exc:
        report['error'] = type(exc).__name__
    if args.local_meta:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from app.channels import meta_instagram
        meta_instagram.get_settings = lambda: SimpleNamespace(
            meta_page_access_token=values.get('META_PAGE_ACCESS_TOKEN') or '',
            meta_ig_business_account_id=values.get('META_IG_BUSINESS_ACCOUNT_ID') or '')
        report['local_meta_credential_probe'] = asyncio.run(meta_instagram.probe_instagram_graph_subscriptions())
        report['local_meta_scope'] = 'existing_local_credential; deployment credential equality not verified'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get('health_http_status') == 200 and report.get('admin_http_status') == 200 else 1


if __name__ == '__main__':
    raise SystemExit(main())
