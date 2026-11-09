"""Question answering over your thesis PDFs (fully local).

    python thesis_qa.py --index              # build the index (run once, or after changing PDFs)
    python thesis_qa.py "What is KG-EDAS?"   # one question
    python thesis_qa.py                      # interactive
"""
import argparse
import hashlib
import json
import math
import re
import sys
import time
from pathlib import Path

import ollama
from pypdf import PdfReader

import config

HERE = Path(__file__).parent
DOCS = HERE / "thesis"
INDEX = HERE / ".cache" / "thesis_index.json"
INDEX.parent.mkdir(exist_ok=True)

EMBED_MODEL = "nomic-embed-text"
MODEL = config.MODEL
CHUNK, OVERLAP, TOP_K = 1000, 150, 5
OPTS = {"temperature": 0.1, "num_ctx": 4096}

SYSTEM = (
    "You answer questions about a thesis using ONLY the numbered excerpts provided. "
    "Cite the excerpts you use as [1], [2]. If the excerpts do not contain the answer, "
    "reply exactly: I could not find this in the thesis. "
    "Do not use outside knowledge. Do not invent numbers, results or citations. "
    "Be concise: at most 150 words."
)


# ------------------------------------------------------------ indexing
def split(text):
    pieces, i = [], 0
    while i < len(text):
        piece = text[i:i + CHUNK]
        if i + CHUNK < len(text):  # prefer to end at a sentence boundary
            cut = piece.rfind(". ")
            if cut > CHUNK * 0.6:
                piece = piece[:cut + 1]
        pieces.append(piece.strip())
        if i + len(piece) >= len(text):
            break
        i += max(len(piece) - OVERLAP, 1)
    return [p for p in pieces if len(p) > 60]


def read_pdfs():
    chunks = []
    for pdf in sorted(DOCS.glob("*.pdf")):
        reader = PdfReader(str(pdf))
        for pno, page in enumerate(reader.pages, 1):
            try:
                text = page.extract_text() or ""
            except Exception:
                continue
            text = re.sub(r"-\n(\w)", r"\1", text)      # join hyphenated line breaks
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) < 80:
                continue
            for piece in split(text):
                chunks.append({"file": pdf.name, "page": pno, "text": piece})
    return chunks


def fingerprint():
    h = hashlib.sha1()
    for pdf in sorted(DOCS.glob("*.pdf")):
        s = pdf.stat()
        h.update(f"{pdf.name}{s.st_size}{int(s.st_mtime)}".encode())
    return h.hexdigest()


def _norm(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def embed(texts, prefix):
    out = []
    for i in range(0, len(texts), 16):
        batch = [prefix + t for t in texts[i:i + 16]]
        try:
            r = ollama.embed(model=EMBED_MODEL, input=batch)
        except Exception as e:
            raise RuntimeError(
                f"Embedding failed: {e}. Did you run: ollama pull {EMBED_MODEL} ? "
                "Is the Ollama app open?") from e
        out += [_norm(v) for v in r.embeddings]
    return out


def build_index(progress=print):
    if not DOCS.exists() or not list(DOCS.glob("*.pdf")):
        raise RuntimeError(f"No PDFs found. Put your thesis PDF in: {DOCS}")
    progress("Reading PDFs…")
    chunks = read_pdfs()
    if not chunks:
        raise RuntimeError("No text found. The PDF may be scanned images (it would need OCR).")
    progress(f"{len(chunks)} chunks. Embedding (1-3 minutes)…")
    vecs = []
    for i in range(0, len(chunks), 64):
        vecs += embed([c["text"] for c in chunks[i:i + 64]], "search_document: ")
        progress(f"  {min(i + 64, len(chunks))}/{len(chunks)}")
    for c, v in zip(chunks, vecs):
        c["vec"] = v
    data = {"fingerprint": fingerprint(), "chunks": chunks}
    INDEX.write_text(json.dumps(data), encoding="utf-8")
    progress("Index saved.")
    return data


def load_index(progress=print):
    """Load the saved index; rebuild if missing or if the PDFs changed."""
    if INDEX.exists():
        try:
            data = json.loads(INDEX.read_text(encoding="utf-8"))
            if data.get("fingerprint") == fingerprint():
                return data
            progress("PDFs changed, rebuilding index…")
        except Exception:
            pass
    return build_index(progress)


# ------------------------------------------------------------ retrieval
def retrieve(question, index, k=TOP_K):
    qv = embed([question], "search_query: ")[0]
    words = {w for w in re.findall(r"[a-z0-9\-]{4,}", question.lower())}
    scored = []
    for c in index["chunks"]:
        sim = sum(a * b for a, b in zip(qv, c["vec"]))
        low = c["text"].lower()
        bonus = 0.05 * (sum(w in low for w in words) / len(words)) if words else 0
        scored.append((sim + bonus, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"file": c["file"], "page": c["page"], "text": c["text"], "score": round(s, 3)}
            for s, c in scored[:k]]


# ------------------------------------------------------------ answering
def ask_stream(question, index, model=None):
    """Yield events: status, sources, token, done, error."""
    model = model or MODEL
    t0 = time.time()
    yield {"type": "status", "text": "Searching the thesis…"}
    try:
        hits = retrieve(question, index)
    except Exception as e:
        yield {"type": "error", "text": str(e)}
        return
    yield {"type": "sources", "sources": hits}

    ctx = "\n\n".join(f"[{i}] ({h['file']}, p.{h['page']}) {h['text']}" for i, h in enumerate(hits, 1))
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"Excerpts:\n{ctx}\n\nQuestion: {question}"}]
    yield {"type": "status", "text": "Writing the answer…"}
    answer = ""
    try:
        for chunk in ollama.chat(model=model, messages=msgs, stream=True, options={**OPTS, "num_predict": 350},
                                 keep_alive=config.KEEP_ALIVE):
            piece = chunk.message.content or ""
            if piece:
                answer += piece
                yield {"type": "token", "text": piece}
    except Exception as e:
        yield {"type": "error", "text": f"{type(e).__name__}: {e}. Is Ollama running?"}
        return
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", answer)}
    yield {"type": "done", "answer": answer.strip(),
           "bad": sorted(n for n in cited if n < 1 or n > len(hits)),
           "seconds": round(time.time() - t0, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="*")
    ap.add_argument("--index", action="store_true", help="rebuild the index")
    args = ap.parse_args()

    try:
        index = build_index() if args.index else load_index()
    except RuntimeError as e:
        print("Error:", e)
        return
    if args.index and not args.question:
        return

    def run(q):
        for ev in ask_stream(q, index):
            if ev["type"] == "sources":
                print("Sources:", ", ".join(f"[{i}] p.{h['page']}" for i, h in enumerate(ev["sources"], 1)))
                print("\nAnswer: ", end="", flush=True)
            elif ev["type"] == "token":
                print(ev["text"], end="", flush=True)
            elif ev["type"] == "error":
                print("Error:", ev["text"])
            elif ev["type"] == "done":
                if ev["bad"]:
                    print(f"\nWARNING: cites missing sources {ev['bad']}")
                print(f"\n({ev['seconds']}s)")

    if args.question:
        run(" ".join(args.question))
        return
    print("Ask about your thesis (or 'quit').")
    while True:
        q = input("\nYou: ").strip()
        if q.lower() in {"quit", "exit", "q"}:
            break
        if q:
            run(q)


if __name__ == "__main__":
    main()