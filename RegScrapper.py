import os, sys, re, hashlib, datetime, time, json
from typing import Optional, Tuple, List
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Use centralized DB and LLM adapter
from db_utils import sb
from llm_adapter import LLMAdapter

load_dotenv(dotenv_path=".env")

# ---------- CONFIG / SOURCES (merged from scrape_sources.py) ----------
SOURCES = [
    # label, url, category, lang
    ("FINTRAC Intro", "https://fintrac-canafe.canada.ca/intro-eng", "General", "en"),
    ("MSB Obligations", "https://fintrac-canafe.canada.ca/msb-esm/msb-eng", "MSB", "en"),
    ("Securities Dealer", "https://fintrac-canafe.canada.ca/re-ed/sec-eng", "Securities", "en"),
    ("National Risk Assessment", "https://fintrac-canafe.canada.ca/businesses-entreprises/assessment-evaluation-eng", "Risk Assessment", "en"),
    ("Risk-Based Approach Guidance", "https://fintrac-canafe.canada.ca/guidance-directives/compliance-conformite/rba/rba-eng", "Guidance", "en"),
    ("Act & Regulations", "https://fintrac-canafe.canada.ca/act-loi/1-eng", "Act", "en"),
]

SOURCE_AUTHORITY = "FINTRAC"
JURISDICTION = "Canada"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
}

# LLM adapter (centralized provider switching; default gemini)
llm = LLMAdapter()

# ---------- UTIL ----------
def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def clean_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    # Drop chrome/boilerplate to reduce false positives
    for sel in ["header", "footer", "nav", "script", "style", "noscript", ".wb-srch", ".gc-subway"]:
        for tag in soup.select(sel):
            tag.decompose()

    main = soup.find("main") or soup.body or soup
    text = main.get_text(separator="\n", strip=True)

    # Normalize whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Remove “Date modified: YYYY-MM-DD” (common on GoC pages)
    lines = []
    for line in text.splitlines():
        if re.search(r"^Date (modified|updated)\s*:\s*\d{4}-\d{2}-\d{2}", line, re.IGNORECASE):
            continue
        lines.append(line)
    return "\n".join(lines).strip()

def fetch_html(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text

# ---------- DB helpers (thin wrappers using sb) ----------
def get_existing_regulation(url: str) -> Optional[dict]:
    res = sb.table("regulations").select("*")\
        .eq("source", SOURCE_AUTHORITY).eq("url", url).limit(1).execute()
    return res.data[0] if res.data else None

def upsert_page(title: str, url: str, lang: str, category: str, content: str, content_hash: str, changed: bool):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    payload = {
        "title": title,
        "source": SOURCE_AUTHORITY,
        "jurisdiction": JURISDICTION,
        "url": url,
        "lang": lang,
        "category": category,
        "content": content,
        "content_hash": content_hash,
        "last_fetched": now,
    }
    if changed:
        payload["last_updated"] = now

    sb.table("regulations").upsert(
        payload,
        on_conflict="source,url"
    ).execute()

def upsert_with_version(title, url, lang, category, content, content_hash, change_summary=None):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    payload = {
        "p_source": SOURCE_AUTHORITY,
        "p_url": url,
        "p_title": title,
        "p_category": category,
        "p_lang": lang,
        "p_jurisdiction": JURISDICTION,
        "p_content": content,
        "p_content_hash": content_hash,
        "p_last_fetched": now,
        "p_change_summary": change_summary or {}
    }
    sb.rpc("upsert_regulation_with_version", payload).execute()

# ---------- Change extraction & AI summarization (merged from openAIAPI.py) ----------
def extract_changed_chunks(old: str, new: str, context_lines: int = 3, min_len: int = 200) -> List[Tuple[str, str]]:
    """Use difflib-like logic to return changed blocks with context."""
    from difflib import SequenceMatcher
    old_lines, new_lines = old.splitlines(), new.splitlines()
    sm = SequenceMatcher(None, old_lines, new_lines)
    chunks = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        i1c = max(0, i1 - context_lines); i2c = min(len(old_lines), i2 + context_lines)
        j1c = max(0, j1 - context_lines); j2c = min(len(new_lines), j2 + context_lines)
        old_chunk = "\n".join(old_lines[i1c:i2c]).strip()
        new_chunk = "\n".join(new_lines[j1c:j2c]).strip()
        if len(old_chunk) + len(new_chunk) >= min_len:
            chunks.append((old_chunk, new_chunk))
    return chunks or [(old[:4000], new[:4000])]

def summarize_meaningful_diff(old_text: str, new_text: str) -> dict:
    """
    Uses centralized LLM adapter to classify changes and return structured JSON.
    Returns {"is_meaningful_change": bool, ...}
    """
    prompt = (
        "You are a Canadian financial compliance analyst for FINTRAC AML obligations (MSBs). "
        "Return STRICT JSON only. Keys: is_meaningful_change (bool), reason (str), "
        "categories (array), changes (array of {section_hint, old_excerpt, new_excerpt, analysis}), "
        "regeneration_required (bool). Ignore punctuation/formatting-only edits.\n\n"
        f"OLD:\n{old_text[:12000]}\n\nNEW:\n{new_text[:12000]}\n\nContext: FINTRAC MSB obligations page."
    )
    resp = llm.generate_text(prompt, max_output_tokens=600, temperature=0.0)
    txt = llm.text_for(resp)
    try:
        parsed = json.loads(txt)
        return parsed
    except Exception:
        # If parsing fails, attempt to get a concise interpretation
        return {"is_meaningful_change": False, "reason": "JSON parse error or unexpected LLM output", "raw": txt}

def log_ai_change(summary: dict):
    sb.table("regulation_change_log").insert({
        "source": SOURCE_AUTHORITY, "title": "MSB Obligations", "summary_json": summary
    }).execute()

# ---------- Scrape / orchestration (merged) ----------
def scrape_one(title: str, url: str, category: str, lang: str, dry_run: bool = False):
    print(f"🔍 Fetching: {title} — {url}")
    html = fetch_html(url)
    text = clean_text(html)
    new_hash = sha256(text)
    print(f"ℹ️ Extracted length: {len(text)} chars")

    existing = get_existing_regulation(url)

    if not existing:
        print("📥 No existing record found. Seeding…")
        if dry_run:
            print("🧪 [DRY-RUN] Would insert:", {"title": title, "url": url, "category": category})
            return
        upsert_page(title, url, lang, category, text, new_hash, changed=True)
        print("✅ Inserted.")
        return

    old_hash = existing.get("content_hash")
    if old_hash == new_hash:
        print("✅ No change detected.")
        if not dry_run:
            upsert_page(title, url, lang, category, existing["content"], existing["content_hash"], changed=False)
        return

    print("🔄 Change detected (hash differs after normalization).")
    if dry_run:
        old_text = existing.get("content", "")
        from itertools import zip_longest
        for a, b in zip_longest(old_text.splitlines(), text.splitlines(), fillvalue=""):
            if a != b:
                print("OLD:", a[:200])
                print("NEW:", b[:200])
                break
        print("🧪 [DRY-RUN] Would update row.")
        return

    # Determine if meaningful using LLM
    summary = summarize_meaningful_diff(existing.get("content", ""), text)

    # Upsert with version + store change summary if meaningful
    upsert_with_version(title=title, url=url, lang=lang, category=category, content=text, content_hash=new_hash, change_summary=summary if summary.get("is_meaningful_change") else None)

    if summary.get("is_meaningful_change"):
        log_ai_change(summary)
        print("Meaningful change → stored + logged.")
    else:
        print("Only superficial edits → stored as fetched (no meaningful change).")

def main(dry_run: bool = False, pause_sec: float = 1.0, verbose: bool = True):
    for (title, url, category, lang) in SOURCES:
        try:
            if verbose:
                print(f"--- {title} ({category}) ---")
            scrape_one(title, url, category, lang, dry_run=dry_run)
        except Exception as e:
            print(f"❗ Error scraping {url}: {e}")
        time.sleep(pause_sec)  # polite pacing

if __name__ == "__main__":
    dry = len(sys.argv) > 1 and sys.argv[1].lower() in {"dry", "dry-run", "--dry-run"}
    main(dry_run=dry)
