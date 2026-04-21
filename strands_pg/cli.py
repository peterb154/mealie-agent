"""Tiny interactive chat client for a running strands-pg agent.

Talks HTTP to the same `/chat` endpoint the rest of the world uses. No SDK
import, no direct DB access — the CLI is a dogfood client, nothing more.

Usage:

    strands-pg-chat                              # localhost:8000, session=cli
    strands-pg-chat --url http://host:8000
    strands-pg-chat --session-id brian@example.com
    strands-pg-chat --prompts                    # list prompts on the agent
    strands-pg-chat --put-prompt rules ./rules.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

DEFAULT_URL = "http://localhost:8000"
DEFAULT_SESSION = "cli"
TIMEOUT_SEC = 120.0


def _chat(url: str, session_id: str) -> int:
    print(f"strands-pg-chat -> {url}  session={session_id}")
    print("type your message. ctrl-d or 'exit' to quit.\n")
    with httpx.Client(base_url=url, timeout=TIMEOUT_SEC) as client:
        try:
            while True:
                try:
                    msg = input("you> ").strip()
                except EOFError:
                    print()
                    return 0
                if not msg:
                    continue
                if msg in {"exit", "quit", ":q"}:
                    return 0
                try:
                    resp = client.post(
                        "/chat",
                        json={"session_id": session_id, "message": msg},
                    )
                except httpx.HTTPError as exc:
                    print(f"! network error: {exc}", file=sys.stderr)
                    continue

                if resp.status_code != 200:
                    print(f"! {resp.status_code}: {resp.text}", file=sys.stderr)
                    continue
                body = resp.json()
                print(f"agent> {body['response']}\n")
        except KeyboardInterrupt:
            print()
            return 0


def _list_prompts(url: str) -> int:
    with httpx.Client(base_url=url, timeout=TIMEOUT_SEC) as client:
        resp = client.get("/prompts")
    resp.raise_for_status()
    for p in resp.json():
        print(f"--- {p['name']} ---")
        print(p["body"].rstrip())
        print()
    return 0


def _put_prompt(url: str, name: str, source: str) -> int:
    path = Path(source)
    body = path.read_text(encoding="utf-8") if path.exists() else source
    with httpx.Client(base_url=url, timeout=TIMEOUT_SEC) as client:
        resp = client.put(f"/prompts/{name}", json={"body": body})
    if resp.status_code != 200:
        print(f"! {resp.status_code}: {resp.text}", file=sys.stderr)
        return 1
    print(json.dumps(resp.json(), indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Chat with a strands-pg agent over HTTP.")
    parser.add_argument(
        "--url", default=DEFAULT_URL, help=f"agent base URL (default: {DEFAULT_URL})"
    )
    parser.add_argument(
        "--session-id", "-s", default=DEFAULT_SESSION, help="session id (default: cli)"
    )
    parser.add_argument("--prompts", action="store_true", help="list prompts and exit")
    parser.add_argument(
        "--put-prompt",
        nargs=2,
        metavar=("NAME", "FILE_OR_TEXT"),
        help="upload a prompt (file path or literal text) and exit",
    )
    args = parser.parse_args()

    if args.prompts:
        return _list_prompts(args.url)
    if args.put_prompt:
        return _put_prompt(args.url, args.put_prompt[0], args.put_prompt[1])
    return _chat(args.url, args.session_id)


if __name__ == "__main__":
    sys.exit(main())
