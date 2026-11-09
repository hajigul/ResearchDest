import argparse
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import ollama

import citecheck
import config
import thesis_qa
from agent import run_agent_stream
from related_work import run_draft

HERE = Path(__file__).parent
STATE = {"status": "idle", "msg": "", "chunks": 0, "files": []}
INDEX = {"data": None}
LOCK = threading.Lock()


def build_index_bg(force=False):
    def work():
        with LOCK:
            STATE.update(status="building", msg="Starting…")
            try:
                prog = lambda m: STATE.update(msg=str(m))
                data = thesis_qa.build_index(prog) if force else thesis_qa.load_index(prog)
                INDEX["data"] = data
                STATE.update(status="ready", msg="", chunks=len(data["chunks"]),
                             files=sorted({c["file"] for c in data["chunks"]}))
            except Exception as e:
                INDEX["data"] = None
                STATE.update(status="error", msg=str(e))
    threading.Thread(target=work, daemon=True).start()


def one_error(msg):
    yield {"type": "error", "text": msg}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _stream(self, gen):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            for ev in gen:
                self.wfile.write((json.dumps(ev) + "\n").encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, (HERE / "suite.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/info":
            try:
                models = [m.model for m in ollama.list().models if "embed" not in m.model]
                online = True
            except Exception:
                models, online = [], False
            pdfs = sorted(p.name for p in thesis_qa.DOCS.glob("*.pdf")) if thesis_qa.DOCS.exists() else []
            self._json({"online": online, "models": models, "default": config.MODEL,
                        "thesis": dict(STATE), "pdfs": pdfs})
        else:
            self._send(404, b"Not found", "text/plain")

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except Exception:
            self._send(400, b"Bad request", "text/plain")
            return
        model = str(body.get("model") or config.MODEL)
        p = self.path

        if p == "/api/search":
            q = str(body.get("question", ""))[:1500].strip()
            mode = body.get("mode") if body.get("mode") in ("fast", "agent") else "fast"
            source = body.get("source") if body.get("source") in ("auto", "arxiv", "openalex") else "auto"
            gen = run_agent_stream(q, [], model, mode, bool(body.get("recent")), source) if q \
                else one_error("Empty question.")
            self._stream(gen)

        elif p == "/api/draft":
            text = str(body.get("text", ""))[:3000].strip()
            papers = body.get("papers")
            if isinstance(papers, list):
                need = ("title", "published", "summary", "link")
                papers = [x for x in papers if isinstance(x, dict) and all(k in x for k in need)] or None
            self._stream(run_draft(text, model, papers) if text else one_error("Empty text."))

        elif p == "/api/cite":
            text = str(body.get("text", ""))[:200000]
            pdf = body.get("pdf") or None
            self._stream(citecheck.run_check(text, pdf, thesis_qa.DOCS))

        elif p == "/api/qa":
            q = str(body.get("question", ""))[:1500].strip()
            if not q:
                self._stream(one_error("Empty question."))
            elif INDEX["data"] is None:
                self._stream(one_error("The thesis index is not ready: " + (STATE["msg"] or STATE["status"])))
            else:
                self._stream(thesis_qa.ask_stream(q, INDEX["data"], model))

        elif p == "/api/reindex":
            if STATE["status"] == "building":
                self._json({"ok": False, "msg": "Already building."}, 409)
            else:
                build_index_bg(force=True)
                self._json({"ok": True})
        else:
            self._send(404, b"Not found", "text/plain")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("port", nargs="?", type=int, default=8003)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)   # local only
    url = f"http://127.0.0.1:{args.port}"
    print(f"ResearchDesk running at {url}  (Ctrl+C to stop)")
    build_index_bg()
    threading.Thread(target=lambda: ollama.generate(model=config.MODEL, prompt="",
                                                    keep_alive=config.KEEP_ALIVE), daemon=True).start()
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()