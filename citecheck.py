"""Citation checker: verifies references against OpenAlex and arXiv."""
import json
import re
import time
import unicodedata
import urllib.parse
from pathlib import Path

from pypdf import PdfReader

import config
from tools import OPENALEX_API, _oa_paper, get_paper_details, http_get

MAX_REFS = 150
SELECT = ("id,doi,display_name,publication_year,publication_date,cited_by_count,"
          "authorships,primary_location,open_access,abstract_inverted_index")
NUM_RE = re.compile(r"(?m)^\s*(?:\[\d+\]|\d{1,3}[.)])\s+")
START_RE = re.compile(r"^(?:[A-Z][A-Za-z'\-]+,\s+[A-Z]\.|(?:[A-Z]\.\s?){1,3}[A-Z][a-z]+)")
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+")
ARXIV_RE = re.compile(r"arxiv[:\s/]*(?:abs/)?(\d{4}\.\d{4,5})", re.I)


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    return re.findall(r"[a-z0-9]+", s)


# ------------------------------------------------------------ parsing
def references_from_pdf(path):
    """Return the text after the 'References' heading of a PDF."""
    reader = PdfReader(str(path))
    pages = []
    for p in reader.pages:
        try:
            pages.append(p.extract_text() or "")
        except Exception:
            pages.append("")
    full = "\n".join(pages)
    for m in re.finditer(r"(?im)^\s*(references|bibliography)\s*$", full):
        tail = full[m.end():m.end() + 4000]
        if len(YEAR_RE.findall(tail)) >= 8:      # skips the table of contents
            return full[m.end():]
    raise ValueError("Could not find a 'References' or 'Bibliography' section in this PDF.")


def _merge_lines(text):
    out, buf = [], ""
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if buf and START_RE.match(line) and re.search(r"[.)\d]$", buf):
            out.append(buf)
            buf = line
        else:
            buf = (buf + " " + line).strip()
    if buf:
        out.append(buf)
    return out


def split_references(text):
    text = re.sub(r"-\n(?=[a-z])", "", text.replace("\r", ""))
    if len(NUM_RE.findall(text)) >= 3:
        parts = NUM_RE.split(text)
    elif re.search(r"\n\s*\n", text):
        parts = re.split(r"\n\s*\n", text)
    else:
        parts = _merge_lines(text)
    refs = [re.sub(r"\s+", " ", p).strip() for p in parts]
    return [r for r in refs if len(r) > 25]


def guess_title(ref):
    m = re.search(r"[\"“”]([^\"“”]{15,})[\"“”]", ref)
    if m:
        return m.group(1)
    segs = [s.strip() for s in re.split(r"\.\s+|\?\s+", ref)][1:]   # first part = authors
    segs = [s for s in segs[:4] if len(s.split()) >= 4]
    return max(segs, key=len) if segs else ""


# ------------------------------------------------------------ lookup
def _search(query):
    params = {"search": query, "per-page": 5, "select": SELECT}
    if config.CONTACT_EMAIL:
        params["mailto"] = config.CONTACT_EMAIL
    data, cached = http_get(OPENALEX_API + urllib.parse.urlencode(params))
    if not cached:
        time.sleep(0.2)
    return [_oa_paper(w, []) for w in json.loads(data).get("results", [])]


def _candidates(ref):
    """Yield lists of candidate papers, most specific lookup first."""
    m = ARXIV_RE.search(ref)
    if m:
        p = get_paper_details(m.group(1))
        if isinstance(p, dict) and "error" not in p and p.get("title"):
            yield [p]
    d = DOI_RE.search(ref)
    if d:
        found = None
        try:
            doi = d.group(0).rstrip(".,;)")
            url = ("https://api.openalex.org/works/doi:" + urllib.parse.quote(doi, safe="/")
                   + "?select=" + SELECT)
            data, _ = http_get(url)
            found = _oa_paper(json.loads(data), [])
        except Exception:
            pass
        if found:
            yield [found]
    clean = DOI_RE.sub("", re.sub(r"https?://\S+", "", NUM_RE.sub("", ref)))
    clean = re.sub(r"\s+", " ", clean).strip()[:300]
    if clean:
        yield _search(clean)
    t = guess_title(ref)
    if t:
        yield _search(t[:250])


def _title_score(ref_tokens, title):
    tt = _norm(title)
    if len(tt) < 2:
        return 0.0
    if " " + " ".join(tt) + " " in " " + " ".join(ref_tokens) + " ":
        return 1.0
    rs = set(ref_tokens)
    return 0.9 * sum(w in rs for w in tt) / len(tt)


def _year(p):
    try:
        return int(str(p.get("published", ""))[:4])
    except ValueError:
        return None


def check_reference(ref):
    rn = _norm(ref)
    ref_years = {int(y) for y in YEAR_RE.findall(ref)}
    best, best_t = None, 0.0
    for papers in _candidates(ref):
        for p in papers:
            s = _title_score(rn, p.get("title", ""))
            if s > best_t:
                best, best_t = p, s
        if best_t >= 0.95:
            break

    res = {"reference": ref, "score": round(best_t, 2), "issues": [], "match": None}
    if best is None or best_t < 0.6:
        res["status"] = "not_found"
        return res

    year = _year(best)
    authors = best.get("authors") or []
    res["match"] = {
        "title": best.get("title", ""), "authors": authors[:3],
        "authors_total": best.get("authors_total", len(authors)), "year": year,
        "venue": best.get("venue", ""), "link": best.get("link", ""),
        "id": best.get("id", ""), "citations": best.get("citations"),
        "source": best.get("source", ""),
    }
    if best_t < 0.85:
        res["status"] = "possible"
        res["issues"].append("only part of the title matches")
        return res

    if ref_years and year and not any(abs(year - y) <= 1 for y in ref_years):
        res["issues"].append(f"year: reference says {sorted(ref_years)[-1]}, record says {year}")
    if authors:
        surname = (_norm(authors[0]) or [""])[-1]
        if surname and surname not in set(rn):
            res["issues"].append(f"first author '{authors[0]}' not found in the reference")
    res["status"] = "check" if res["issues"] else "verified"
    return res


def run_check(text="", pdf=None, docs_dir=None):
    """Yield events: status, parsed, item, done, error."""
    t0 = time.time()
    yield {"type": "status", "text": "Reading references…"}
    try:
        if pdf:
            path = Path(docs_dir) / Path(pdf).name
            if not path.exists():
                raise ValueError(f"{Path(pdf).name} not found in the thesis folder.")
            text = references_from_pdf(path)
        refs = split_references(text)
    except Exception as e:
        yield {"type": "error", "text": str(e)}
        return
    if not refs:
        yield {"type": "error", "text": "No references found. Paste one reference per line, or a numbered list."}
        return
    total = len(refs)
    refs = refs[:MAX_REFS]
    yield {"type": "parsed", "count": len(refs), "capped": total > MAX_REFS, "total": total}

    counts = {}
    for i, ref in enumerate(refs, 1):
        yield {"type": "status", "text": f"Checking reference {i} of {len(refs)}…"}
        try:
            r = check_reference(ref)
        except Exception as e:
            r = {"status": "error", "reference": ref, "score": 0, "match": None,
                 "issues": [f"lookup failed: {type(e).__name__}: {e}"]}
        r["n"] = i
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        yield {"type": "item", "item": r, "done": i, "total": len(refs)}
    yield {"type": "done", "counts": counts, "seconds": round(time.time() - t0, 1)}