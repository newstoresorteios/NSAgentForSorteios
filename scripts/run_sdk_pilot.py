"""Run the isolated read-only SDK pilot using published model and budget controls."""
import argparse,asyncio,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    from app.agents.sdk_pilot import run_readonly_pilot
    from app.persona.persona_runtime import load_persona_runtime
    from app.configuration.runtime import bind_bundle,reset_bundle,settings_from_bundle
    from app.config import get_settings
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workspace',required=True);p.add_argument('--question',required=True)
    args=p.parse_args();persona=load_persona_runtime(workspace_id=args.workspace)
    bundle=persona.configuration_bundle
    token=bind_bundle(bundle,settings_from_bundle(get_settings(),bundle))
    try:
        result=asyncio.run(run_readonly_pilot(args.question))
        data=json.loads(result.final_output)
        print(json.dumps({'documents':[{'slug':d.get('slug'),'source_url':d.get('source_url')}
                                      for d in data.get('documents',[])],
                          'model_calls':len(result.raw_responses),
                          'total_tokens':sum(r.usage.total_tokens for r in result.raw_responses)},ensure_ascii=False))
    finally:reset_bundle(token)


if __name__=='__main__':main()
