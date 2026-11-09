import argparse
import json
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import ollama

import config
from agent import MODEL, run_agent_stream

HERE = Path(__file__).parent


def prewarm(model):
    """Load the model into memory now so the first question is not slow."""
    try:
        ollama.generate(model=model, prompt="", keep_alive=config.KEEP_ALIVE)
    except Exception:
        pass


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "your-ip"
    finally:
        s.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/models":
            try:
                models = [m.model for m in ollama.list().models]
            except Exception:
                models = []
            self._send(200, json.dumps({"models": models, "default": MODEL}).encode(), "application/json")
        else:
            self._send(404, b"Not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/chat":
            self._send(404, b"Not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._send(400, b"Bad request", "text/plain")
            return

        question = str(body.get("question", ""))[:2000].strip()
        if not question:
            self._send(400, b"Empty question", "text/plain")
            return
        model = str(body.get("model") or MODEL)
        mode = body.get("mode") if body.get("mode") in ("fast", "agent") else "fast"
        source = body.get("source") if body.get("source") in ("auto", "arxiv", "openalex") else "auto"
        recent = bool(body.get("recent"))
        history = [
            {"role": h["role"], "content": h["content"][:3000]}
            for h in body.get("history", [])
            if isinstance(h, dict) and h.get("role") in ("user", "assistant")
            and isinstance(h.get("content"), str)
        ][-10:]

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            for ev in run_agent_stream(question, history, model, mode, recent, source):
                self.wfile.write((json.dumps(ev) + "\n").encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser closed or the user pressed Stop


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("port", nargs="?", type=int, default=config.PORT)
    ap.add_argument("--lan", action="store_true", help="let other devices on your network connect")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    host = "0.0.0.0" if args.lan else "127.0.0.1"
    server = ThreadingHTTPServer((host, args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"ScholarAgent running at {url}  (Ctrl+C to stop)")
    if args.lan:
        print(f"LAN access: http://{lan_ip()}:{args.port}")
        print("Warning: there is no login. Anyone on this network can use your Mac's compute.")

    threading.Thread(target=prewarm, args=(MODEL,), daemon=True).start()
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()