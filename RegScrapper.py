import os, sys, re, datetime
import requests
from bs4 import BeautifulSoup
from supabase import create_client, Client
from dotenv import load_dotenv
from datetime import datetime, timezone
import json
from openai import OpenAI

FINTRAC_URL = "https://fintrac-canafe.canada.ca/msb-esm/msb-eng"
TITLE = "MSB Obligations"
SOURCE = "FINTRAC"

HEADERS = {
    # A friendly UA helps avoid occasional 403s on gov sites
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
}

def fetch_html(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text

def clean_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    # Drop chrome
    for sel in ["header", "footer", "nav", "script", "style", "noscript", ".wb-srch", ".gc-subway"]:
        for tag in soup.select(sel):
            tag.decompose()

    main = soup.find("main") or soup.body or soup
    text = main.get_text(separator="\n", strip=True)

    # Normalize whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Remove “Date modified: YYYY-MM-DD”
    lines = []
    for line in text.splitlines():
        if re.search(r"^Date (modified|updated)\s*:\s*\d{4}-\d{2}-\d{2}", line, re.IGNORECASE):
            continue
        lines.append(line)
    return "\n".join(lines).strip()

def connect_supabase() -> Client:
    load_dotenv(dotenv_path=".env")
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        print("❌ Missing SUPABASE_URL or SUPABASE_KEY env vars.")
        sys.exit(1)
    return create_client(url, key)

def get_existing(sb: Client):
    res = sb.table("regulations").select("*")\
        .eq("source", SOURCE).eq("title", TITLE).execute()
    return res.data[0] if res.data else None

def upsert_regulation(sb: Client, content: str, dry_run: bool=False):
    now = datetime.now(timezone.utc).isoformat()

    payload = {
        "title": TITLE,
        "source": SOURCE,
        "jurisdiction": "Canada",
        "category": "MSB",
        "content": content,
        "last_updated": now
    }
    if dry_run:
        print("🧪 [DRY-RUN] Would upsert:", {k: payload[k] for k in ["title","source","jurisdiction","category"]})
        print(f"🧪 [DRY-RUN] Content length: {len(content)} chars")
        return

    # Upsert by unique (source, title)
    sb.table("regulations").upsert(payload, on_conflict="source,title").execute()
    print("✅ Upserted current FINTRAC MSB content.")

def summarize_meaningful_diff(old_text: str, new_text: str) -> dict:
    """Returns STRICT JSON about policy-relevant changes, or {"is_meaningful_change": false}."""
    system = (
        "You are a Canadian financial compliance analyst for FINTRAC AML obligations (MSBs). "
        "Return STRICT JSON only. Keys: is_meaningful_change (bool), reason (str), "
        "categories (array), changes (array of {section_hint, old_excerpt, new_excerpt, analysis}), "
        "regeneration_required (bool). Ignore punctuation/formatting-only edits."
    )
    user = f"OLD:\n{old_text[:12000]}\n\nNEW:\n{new_text[:12000]}\n\nContext: FINTRAC MSB obligations page."

    resp = oai.chat.completions.create(
        model="gpt-4o-mini",  # cheap + good; upgrade to gpt-4o if you like
        temperature=0.0,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    out = resp.choices[0].message.content
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"is_meaningful_change": False, "reason": "JSON parse error"}

def log_ai_change(sb, summary: dict):
    sb.table("regulation_change_log").insert({
        "source": "FINTRAC", "title": "MSB Obligations", "summary_json": summary
    }).execute()

def main(dry_run: bool=False, verbose: bool=True):
    print("🔍 Fetching FINTRAC page...")
    html = fetch_html(FINTRAC_URL)
    text = clean_text(html)

    if verbose:
        preview = text[:600].replace("\n", " ") + ("..." if len(text) > 600 else "")
        print(f"ℹ️ Extracted text length: {len(text)}")
        print(f"ℹ️ Preview: {preview}")

    sb = connect_supabase()
    existing = get_existing(sb)

    if not existing:
        print("📥 No existing record found. Seeding…")
        upsert_regulation(sb, text, dry_run=dry_run)
        return

    old = existing.get("content") or ""
    if old == text:
        print("✅ No textual change detected (post-normalization).")
        return

    # Show a tiny diff hint—human-friendly for testing
    if verbose:
        print("🔄 Detected raw change after normalization.")
        from itertools import zip_longest
        for i, (a, b) in enumerate(zip_longest(old.splitlines(), text.splitlines(), fillvalue="")):
            if a != b:
                print("OLD:", a[:200])
                print("NEW:", b[:200])
                break

    # Use OpenAI to summarize meaningful differences
    summary = summarize_meaningful_diff(old, text)
    if summary.get("is_meaningful_change"):
        if not dry_run:
            upsert_regulation(sb, text, dry_run=False)
            log_ai_change(sb, summary)
        print("Meaningful change → stored + logged.")
    else:
        print("Only superficial edits → no update.")

if __name__ == "__main__":
    # Usage:
    #   python scrape_fintrac_msb.py           -> live upsert
    #   python scrape_fintrac_msb.py dry-run   -> no DB writes
    dry = len(sys.argv) > 1 and sys.argv[1].lower() in {"dry", "dry-run", "--dry-run"}
    main(dry_run=dry)
