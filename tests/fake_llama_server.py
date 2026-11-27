"""Tiny stand-in for llama-server used by tests (not collected as a test module).

Behaviour is chosen by the *content* of the file passed with ``-m``:
  ok     - stream "Hello from FileBot" as three chunks
  tool   - first request streams a split search_files tool call; once a tool
           message is present, stream a final answer that echoes it
  sketch - reply with a plain-text JSON tool sketch (no structured call)
  slow   - stream one chunk, then hang
  crash  - exit immediately with code 3
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main() -> None:
    args = sys.argv[1:]
    mode = open(args[args.index("-m") + 1], encoding="utf-8").read().strip()
    port = int(args[args.index("--port") + 1])
    host = args[args.index("--host") + 1]
    if mode == "crash":
        print("fake engine: model failed to load", flush=True)
        raise SystemExit(3)

    def chunk(delta: dict) -> bytes:
        return b"data: " + json.dumps({"choices": [{"delta": delta}]}).encode() + b"\n\n"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def do_GET(self):
            if self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
            else:
                self.send_error(404)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            out = self.wfile
            messages = body["messages"]
            tool_msgs = [m for m in messages if m["role"] == "tool"]
            if mode == "tool" and not tool_msgs:
                out.write(chunk({"tool_calls": [{"index": 0, "id": "c1", "function": {
                    "name": "search_files", "arguments": '{"query": '}}]}))
                out.write(chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"budget"}'}}]}))
            elif mode == "tool":
                out.write(chunk({"content": "Found: " + tool_msgs[-1]["content"][:40]}))
            elif mode == "sketch" and not tool_msgs:
                out.write(chunk({"content": '{"name":"search_files",'}))
                out.write(chunk({"content": '"arguments":{"query":"budget"}}'}))
            elif mode == "sketch":
                out.write(chunk({"content": "Sketch handled"}))
            elif mode == "slow":
                out.write(chunk({"content": "partial"}))
                out.flush()
                time.sleep(60)
            else:
                for piece in ("Hello ", "from ", "FileBot"):
                    out.write(chunk({"content": piece}))
            out.write(b"data: [DONE]\n\n")
            out.flush()

    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
