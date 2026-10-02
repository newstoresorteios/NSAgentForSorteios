"""Interactive admin preview. Never sends WhatsApp/Instagram messages."""
from __future__ import annotations

import argparse
import os
import sys
import httpx
from dotenv import dotenv_values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="NSAgent base URL")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--env-file", default=".env", help="Ignored file containing ADMIN_API_TOKEN")
    args = parser.parse_args()
    token = os.getenv("ADMIN_API_TOKEN") or dotenv_values(args.env_file).get("ADMIN_API_TOKEN")
    if not token or token == "[SENSITIVE]":
        print("ADMIN_API_TOKEN não está disponível no ambiente/arquivo selecionado.")
        return 1
    if not args.url.startswith("https://") and not args.url.startswith(("http://127.0.0.1:", "http://localhost:")):
        print("Use HTTPS ou um servidor local.")
        return 1
    session = None
    print("Teste do agente direto — sem envio a clientes. /novo reinicia; /sair encerra.")
    with httpx.Client(timeout=100, follow_redirects=False) as client:
        while True:
            try:
                text = input("Você: ").strip()
            except (EOFError, KeyboardInterrupt):
                return 0
            if text == "/sair":
                return 0
            if text == "/novo":
                session = None
                print("Nova conversa.")
                continue
            if not text:
                continue
            try:
                response = client.post(args.url.rstrip("/") + "/api/test/direct",
                    headers={"Authorization": "Bearer " + token},
                    json={"workspace_id": args.workspace, "text": text, "session": session})
                if response.status_code != 200:
                    print(f"Teste indisponível (HTTP {response.status_code}).")
                    continue
                result = response.json()
                session = result["session"]
                print("Agente:", result["reply_text"])
                metrics = result.get("metrics") or {}
                print(f"[{metrics.get('model')} | {metrics.get('calls')} chamadas | {metrics.get('latency_ms')} ms]")
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                print("Falha no teste:", type(exc).__name__)


if __name__ == "__main__":
    sys.exit(main())
