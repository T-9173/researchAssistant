from __future__ import annotations
import os, time, re, html, urllib.parse, logging, string
from queue import Queue
import feedparser, requests, google.generativeai as genai

ARXIV_API = "http://export.arxiv.org/api/query"
S2_API = "https://api.semanticscholar.org/graph/v1"
REL_LIMIT = 4
GEMINI_MODEL = "gemini-1.5-flash-latest"
DELAY_S = 1.1
MAX_PER_QUERY_ARXIV = 50
MAX_PER_QUERY_S2 = 100
UA = {"User-Agent": "ResearchAssistantApp/1.0 (mailto:you@example.com)"}

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(asctime)s %(message)s", datefmt="%H:%M:%S")

def _once(fn):
    done = False
    value = None
    def _wrapper(*a, **kw):
        nonlocal done, value
        if not done:
            value, done = fn(*a, **kw), True
        return value
    return _wrapper

@_once
def _configure_gemini() -> bool:
    k = os.getenv("GOOGLE_API_KEY")
    if not k:
        logging.warning("GOOGLE_API_KEY not set")
        return False
    try:
        genai.configure(api_key=k)
        return True
    except Exception as e:
        logging.error("Gemini configure: %s", e)
        return False

def _s2_key() -> str | None:
    return os.getenv("SEMANTIC_API")

def _gemini(prompt: str) -> str:
    if not prompt.strip() or not _configure_gemini():
        return ""
    try:
        rsp = genai.GenerativeModel(GEMINI_MODEL).generate_content(prompt)
        return getattr(rsp, "text", None) or (rsp.parts[0].text if rsp.parts else "")
    except Exception as e:
        logging.error("Gemini: %s", e)
        return ""

def gemini_essay(abs_: str) -> str:
    p = "Analyze the following research‑paper abstract and write an extremely detailed analytical essay:\n---\n"+abs_+"\n---\nAnalytical Essay:"
    return _gemini(p).strip()

def gemini_related(abs_: str) -> list[dict]:
    prompt = f"List {REL_LIMIT} research papers closely related to the following abstract. Output each on a new line as <ID>::<Title>. If you know the arXiv ID start with arXiv:ID, if you know the Semantic Scholar paperId start with S2:ID, otherwise write Unknown::Title.\n---\n{abs_}\n---\nLines:"
    raw = _gemini(prompt)
    print(raw)
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    out = []
    for line in lines:
        if "::" not in line:
            continue
        pid, title = map(str.strip, line.split("::", 1))
        source = "Unknown"
        if pid.startswith("arXiv:") or re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", pid):
            pid = pid.replace("arXiv:", "")
            source = "arXiv"
            out.append({"paperId": f"arXiv:{pid}", "title": title, "source": source})
        elif pid.startswith("S2:"):
            source = "Semantic Scholar"
            out.append({"paperId": pid, "title": title, "source": source})
        else:
            title_only = f"{pid} {title}".strip()
            entry = _lookup_title(title_only)
            if entry:
                out.append(entry)
        if len(out) == REL_LIMIT:
            break
    return out

def _keywords(text: str) -> list[str]:
    stop = {"the","and","of","to","in","a","for","on","with","an","by",
            "is","that","this","we","at","as","from","be","are","it","or"}
    return [w.strip(string.punctuation)
            for w in text.lower().split()
            if w not in stop and len(w) > 2][:10]


def _fallback_arxiv(text: str, limit: int) -> list[dict]:
    words = _keywords(text)
    if not words:
        return []
    q = urllib.parse.urlencode({"search_query": "all:" + "+AND+".join(words), "start": 0, "max_results": limit})
    feed = feedparser.parse(f"{ARXIV_API}?{q}", request_headers=UA)
    res = []
    for e in getattr(feed, "entries", [])[:limit]:
        raw_id = e.id.split("/")[-1]
        m = re.match(r"(\d{4}\.\d{4,5}(v\d+)?)", raw_id)
        pid = m.group(1) if m else raw_id
        res.append({"paperId": f"arXiv:{pid}", "title": e.title.strip().replace("\n", " "), "source": "arXiv"})
    return res

def _fallback_s2(text: str, limit: int) -> list[dict]:
    key = _s2_key()
    if not key:
        return []
    words = _keywords(text)
    if not words:
        return []
    query = " ".join(words)
    results = _s2_search(query, limit, key)
    out = []
    for r in results[:limit]:
        out.append({"paperId": f"S2:{r['paperId']}", "title": r["title"].strip(), "source": "Semantic Scholar"})
    return out

def _lookup_title(title: str) -> dict | None:
    key = _s2_key()
    if key:
        res = _s2_search(title, 1, key)
        if res:
            r = res[0]
            return {"paperId": f"S2:{r['paperId']}", "title": r["title"].strip(), "source": "Semantic Scholar"}
    rid = _find_arxiv_id_by_title(title)
    if rid:
        return {"paperId": f"arXiv:{rid}", "title": title, "source": "arXiv"}
    return None

def _find_arxiv_id_by_title(title: str) -> str | None:
    if not title:
        return None
    safe = title.replace('"', '')
    q = urllib.parse.urlencode({"search_query": f'ti:"{safe}"', "start": 0, "max_results": 1})
    feed = feedparser.parse(f"{ARXIV_API}?{q}", request_headers=UA)
    if getattr(feed, "entries", []):
        rid = feed.entries[0].id.split("/")[-1]
        m = re.match(r"(\d{4}\.\d{4,5}(v\d+)?)", rid)
        return m.group(1) if m else rid
    return None

def _clean_html(txt: str) -> str:
    return html.unescape(re.sub(r"<.*?>", "", txt or "")).strip()

def _safe_related(abs_: str, title: str) -> list[dict]:
    rel = gemini_related(abs_)
    if not rel:
        rel = _fallback_s2(abs_, REL_LIMIT)
    if len(rel) < REL_LIMIT:
        rel += _fallback_arxiv(abs_, REL_LIMIT - len(rel))
    if len(rel) < REL_LIMIT:
        rel += _fallback_s2(title, REL_LIMIT - len(rel))
    if len(rel) < REL_LIMIT:
        rel += _fallback_arxiv(title, REL_LIMIT - len(rel))
    if not rel:
        rel = [{"paperId": "N/A", "title": "No related papers found", "source": "N/A"}]
    return rel[:REL_LIMIT]


def _mk_arxiv(e) -> dict | None:
    if not all(getattr(e, k, None) for k in ("id", "title", "summary", "link", "authors")):
        return None
    rid = e.id.split("/")[-1]
    m = re.match(r"(\d{4}\.\d{4,5}(v\d+)?)", rid)
    pid = m.group(1) if m else rid
    paper = {
        "paperId": f"arXiv:{pid}",
        "url": e.link,
        "title": e.title.strip().replace("\n", " "),
        "abstract": _clean_html(e.summary),
        "authors": [{"name": a.name} for a in e.authors],
        "year": e.published_parsed.tm_year if getattr(e, "published_parsed", None) else None,
        "venue": getattr(e, "arxiv_primary_category", {}).get("term", "arXiv"),
        "citationCount": 0,
        "influentialCitationCount": 0,
        "references": [],
        "citations": [],
        "insights": "",
        "source": "arXiv"
    }
    paper["references"] = _safe_related(paper["abstract"], paper["title"])
    return paper

def _mk_s2(entry: dict) -> dict | None:
    if not entry.get("paperId") or not entry.get("title"):
        return None
    paper = {
        "paperId": f"S2:{entry['paperId']}",
        "url": entry.get("url"),
        "title": entry["title"].strip().replace("\n", " "),
        "abstract": entry.get("abstract", ""),
        "authors": entry.get("authors", []),
        "year": entry.get("year"),
        "venue": (entry.get("venue") or "Semantic Scholar").strip(),
        "citationCount": entry.get("citationCount", 0) or 0,
        "influentialCitationCount": entry.get("influentialCitationCount", 0) or 0,
        "references": [],
        "citations": [],
        "insights": "",
        "source": "Semantic Scholar"
    }
    paper["references"] = _safe_related(paper["abstract"], paper["title"])
    return paper

def _s2_search(query: str, limit: int, key: str) -> list[dict]:
    headers = {"x-api-key": key, **UA}
    params = {"query": query, "limit": min(limit, MAX_PER_QUERY_S2), "fields": "paperId,url,title,abstract,authors,year,venue,citationCount,influentialCitationCount"}
    try:
        r = requests.get(f"{S2_API}/paper/search", headers=headers, params=params, timeout=20)
        r.raise_for_status()
        return [d for d in r.json().get("data", []) if d.get("paperId") and d.get("title")]
    except requests.RequestException as e:
        logging.error("S2 search: %s", e)
        return []

def _s2_details(pid: str, key: str) -> dict | None:
    headers = {"x-api-key": key, **UA}
    fields = "paperId,url,title,abstract,authors,year,venue,citationCount,influentialCitationCount"
    try:
        r = requests.get(f"{S2_API}/paper/{pid}", headers=headers, params={"fields": fields}, timeout=20)
        r.raise_for_status()
        return _mk_s2(r.json())
    except requests.RequestException as e:
        logging.error("S2 details: %s", e)
        return None

def _arxiv_search(query: str, limit: int):
    if limit <= 0:
        return []
    p = urllib.parse.urlencode({"search_query": f"all:{query}", "start": 0, "max_results": min(limit, MAX_PER_QUERY_ARXIV), "sortBy": "relevance", "sortOrder": "descending"})
    feed = feedparser.parse(f"{ARXIV_API}?{p}", request_headers=UA)
    return getattr(feed, "entries", [])[:limit]

def search_papers_backend(query: str, n: int, q: Queue):
    q.put(("status", f"Searching “{query}”…"))
    key = _s2_key()
    want_s2 = n // 2 if key else 0
    s2_results = _s2_search(query, want_s2, key) if want_s2 else []
    need_arxiv = n - len(s2_results)
    arxiv_results = _arxiv_search(query, need_arxiv)
    total = len(s2_results) + len(arxiv_results)
    if not total:
        q.put(("status", "No papers found"))
        q.put(("papers", []))
        q.put(None)
        return
    papers = []
    done = 0
    for e in arxiv_results:
        done += 1
        q.put(("status", f"Processing {done}/{total} (arXiv)"))
        p = _mk_arxiv(e)
        if p:
            p["insights"] = gemini_essay(p["abstract"]) if p["abstract"] else ""
            papers.append(p)
        time.sleep(DELAY_S)
    for e in s2_results:
        done += 1
        q.put(("status", f"Processing {done}/{total} (Semantic)"))
        p = _mk_s2(e)
        if p:
            p["insights"] = gemini_essay(p["abstract"]) if p["abstract"] else ""
            papers.append(p)
        time.sleep(DELAY_S)
    q.put(("papers", papers))
    q.put(("status", f"Analysis complete ({len(papers)} papers)"))
    q.put(None)

def fetch_paper_details_backend(pid: str, q: Queue):
    if not pid:
        q.put(("paper_details_error", "Invalid ID"))
        return
    q.put(("status", f"Fetching {pid}…"))
    out = None
    if pid.startswith("arXiv:"):
        qs = urllib.parse.urlencode({"id_list": pid[6:], "max_results": 1})
        feed = feedparser.parse(f"{ARXIV_API}?{qs}", request_headers=UA)
        if getattr(feed, "entries", []):
            out = _mk_arxiv(feed.entries[0])
    elif pid.startswith("S2:"):
        key = _s2_key()
        if not key:
            q.put(("paper_details_error", "SEMANTIC_API missing"))
            return
        out = _s2_details(pid[3:], key)
    if out and out.get("abstract"):
        out["insights"] = gemini_essay(out["abstract"])
    if out:
        q.put(("paper_details", out))
        q.put(("status", f"Details ready for {pid}"))
    else:
        q.put(("paper_details_error", f"Details not found for {pid}"))
