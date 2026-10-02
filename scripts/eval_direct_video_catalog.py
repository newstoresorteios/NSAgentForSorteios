"""Authenticated live preview; no customer sends, tokens or sessions in evidence."""
import argparse
import base64
import json
import subprocess
import tempfile
from pathlib import Path

from dotenv import dotenv_values


def main():
    parser = argparse.ArgumentParser()
    for name in ('url', 'workspace', 'env-file', 'vercel-cli', 'video', 'output'):
        parser.add_argument('--'+name, required=True)
    args = parser.parse_args()
    token = dotenv_values(args.env_file)['ADMIN_API_TOKEN']
    session = None
    evidence = []

    def turn(text, video=None, reset=False):
        nonlocal session
        body = dict(workspace_id=args.workspace, channel='instagram', text=text,
                    session=None if reset else session)
        if video:
            body['video_base64'] = base64.b64encode(Path(video).read_bytes()).decode()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'request.json'
            path.write_text(json.dumps(body), encoding='utf-8')
            result = subprocess.run(['node', args.vercel_cli, 'curl', '/api/test/direct',
                '--deployment', args.url, '--', '-sS', '--max-time', '150', '-X', 'POST',
                '-H', 'Content-Type: application/json', '-H', 'Authorization: Bearer '+token,
                '--data-binary', '@'+str(path)], capture_output=True, text=True, encoding='utf-8', timeout=170)
        try:
            data = json.loads(result.stdout)
        except ValueError:
            raise RuntimeError('preview_invalid_response') from None
        if not data.get('ok'):
            raise RuntimeError('preview_failed: '+str(data.get('detail', 'unknown')))
        assert data['sent_to_customer'] is False
        session = data.pop('session')
        evidence.append(dict(input=text, **data))
        Path(args.output).write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'reply':data['reply_text'], 'frames':data.get('media_frames'),
                          'tools':data['metrics']['tools'], 'candidates':data['metrics'].get('overview_candidate_count')}, ensure_ascii=False), flush=True)
        return data

    broad = turn('Quero um Seiko com mostrador verde à pronta entrega. Compare todas as opções disponíveis antes de sugerir. Se não houver, explique as alternativas.')
    assert broad['metrics']['overview_candidate_count'] > 50
    assert 'get_ready_delivery_candidate' in broad['metrics']['tools']
    photo = turn('Me manda a foto da primeira opção que você recomendou.')
    assert photo.get('image_url') and 'prepare_product_image' in photo['metrics']['tools']
    video = turn('Descreva a forma da caixa e o que consegue observar neste vídeo. Não precisa pesquisar catálogo.', args.video, reset=True)
    assert video['media_frames'] > 0, 'Native video frames unavailable'
    assert 'retangular' in video['reply_text'].lower(), 'Expected rectangular watch fixture'
    assert len(video['reply_text']) <= 2000
    print('PASS: full catalog, follow-up photo and native video preview; no customer sends.')


if __name__ == '__main__':
    main()
