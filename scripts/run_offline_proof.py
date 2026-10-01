"""Run pytest with no production credentials, dotenv loading, or external sockets."""
from __future__ import annotations

import os
import argparse
from pathlib import Path
import socket
import sys
import tempfile


def main():
    helper_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--repository', default=helper_root.name,
                        choices=['NSAgentForSorteios', 'Chatbo-backendAgent', 'TRAYadaptor'])
    args, test_args = parser.parse_known_args()
    root = helper_root.parent / args.repository
    os.chdir(root)
    sys.path.insert(0, str(root))
    for key in list(os.environ):
        if any(part in key.upper() for part in (
            'DATABASE_URL', 'SUPABASE', 'API_KEY', 'ACCESS_TOKEN', 'SECRET',
            'ADAPTER_TOKEN', 'NSAGENT_TEST_DATABASE',
        )):
            os.environ.pop(key, None)
    os.environ.update(ENVIRONMENT='test', DRY_RUN='true', AUTO_CREATE_TABLES='false')
    import dotenv
    import dotenv.main
    dotenv.load_dotenv = dotenv.main.load_dotenv = lambda *a, **k: False
    dotenv.dotenv_values = dotenv.main.dotenv_values = lambda *a, **k: {}
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex

    def guarded(real, sock, address):
        if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1', 'localhost') and int(address[1]) >= 32768:
            return real(sock, address)
        raise RuntimeError('offline_proof_blocks_external_network')

    socket.socket.connect = lambda self, address: guarded(connect, self, address)
    socket.socket.connect_ex = lambda self, address: guarded(connect_ex, self, address)
    try:
        import psycopg
        def blocked_db(*args, **kwargs):
            raise RuntimeError('offline_proof_blocks_database')
        psycopg.connect = blocked_db
    except ImportError:
        pass
    import pytest
    temp_root = helper_root / '.proof-temp'
    temp_root.mkdir(exist_ok=True)
    base = tempfile.mkdtemp(prefix='run-', dir=temp_root)
    return pytest.main(['-p', 'no:cacheprovider', '--basetemp=' + base,
                        *(test_args or ['-q', 'tests'])])


if __name__ == '__main__':
    raise SystemExit(main())
