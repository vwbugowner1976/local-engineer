#!/usr/bin/env python3
"""Small Japanese chat web UI for the local Bonsai OpenAI-compatible server."""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
INDEX = WEB / "index.html"

BONSAl_API_BASE = os.environ.get("BONSAI_API_BASE", "http://127.0.0.1:8080/v1").rstrip("/")
BONSAl_MODEL = os.environ.get("BONSAI_MODEL_NAME", "").strip()
WEB_TOKEN = os.environ.get("LOCAL_ENGINEER_WEB_TOKEN", "").strip()
MAX_BODY = 256 * 1024

SYSTEM_PROMPT = """あなたはMac mini上で動作しているローカルAIアシスタントです。
ユーザーとの会話は原則として日本語で行ってください。
技術用語、ファイル名、コード、コマンド、エラーメッセージは必要に応じて原文を維持してください。
回答は簡潔で実用的にしてください。
まだ実行していない操作を実行したかのように報告しないでください。
このチャットの段階では、ユーザーから明示的に依頼されていないファイル変更やコマンド実行は行わず、質問への回答に集中してください。"""


def json_response(handler: BaseHTTPRequestHandler, payload: object, status: int = 200) -> None:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalEngineerBonsaiChat/0.2"

    def authorized(self) -> bool:
        if not WEB_TOKEN:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {WEB_TOKEN}"

    def do_GET(self) -> None:
        if not self.authorized():
            json_response(self, {"error": "unauthorized"}, 401)
            return
        if self.path in ("/", "/index.html"):
            try:
                raw = INDEX.read_bytes()
            except OSError as exc:
                json_response(self, {"error": str(exc)}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if self.path == "/health":
            json_response(self, {"ok": True, "bonsai_api": BONSAl_API_BASE})
            return
        json_response(self, {"error": "not found"}, 404)

    def do_POST(self) -> None:
        if not self.authorized():
            json_response(self, {"error": "unauthorized"}, 401)
            return
        if self.path != "/api/chat":
            json_response(self, {"error": "not found"}, 404)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            json_response(self, {"error": "invalid request size"}, 400)
            return

        try:
            body = json.loads(self.rfile.read(length))
            messages = body["messages"]
            if not isinstance(messages, list) or not messages:
                raise ValueError("messages must be a non-empty array")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            json_response(self, {"error": f"invalid request: {exc}"}, 400)
            return

        clean_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for message in messages[-40:]:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            content = message.get("content")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                clean_messages.append({"role": role, "content": content})

        payload = {
            "messages": clean_messages,
            "temperature": 0.2,
            "stream": True,
        }
        if BONSAl_MODEL:
            payload["model"] = BONSAl_MODEL

        request = urllib.request.Request(
            f"{BONSAl_API_BASE}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Cache-Control": "no-cache",
            },
            method="POST",
        )

        # Proxy Bonsai SSE as SSE.  Sending heartbeat comments while Bonsai is
        # reasoning keeps browser/proxy connections alive before content arrives.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def send_event(payload: object) -> None:
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            self.wfile.write(f"data: {raw}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                usage = None
                model = BONSAl_MODEL or "Bonsai"
                # Tell the browser immediately that the upstream request is alive.
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()

                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if line == "[DONE]":
                        break
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if chunk.get("model"):
                        model = chunk["model"]
                    if chunk.get("usage"):
                        usage = chunk["usage"]

                    delta = ""
                    try:
                        delta = chunk["choices"][0].get("delta", {}).get("content", "") or ""
                    except (KeyError, IndexError, TypeError):
                        pass

                    if delta:
                        send_event({"delta": delta})

                send_event({"done": True, "model": model, "usage": usage})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                send_event({"error": f"Bonsai HTTP {exc.code}", "detail": detail[-4000:]})
            except (BrokenPipeError, ConnectionResetError):
                pass
        except (urllib.error.URLError, TimeoutError) as exc:
            try:
                send_event({"error": f"Bonsai connection failed: {exc}"})
            except (BrokenPipeError, ConnectionResetError):
                pass
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt: str, *args: object) -> None:
        print("[web-chat] " + fmt % args, flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Engineer Bonsai Japanese chat")
    parser.add_argument("--host", default=os.environ.get("LOCAL_ENGINEER_WEB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("LOCAL_ENGINEER_WEB_PORT", "8780")))
    args = parser.parse_args()
    print(f"Local Engineer Bonsai Chat: http://{args.host}:{args.port}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
