import os, sys, re, hashlib, datetime, time
import requests
from bs4 import BeautifulSoup
from supabase import create_client, Client
from dotenv import load_dotenv

# ---------- CONFIG ----------
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

def connect_supabase() -> Client:
    load_dotenv()
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        print("❌ Missing SUPABASE_URL or SUPABASE_KEY env vars.")
        sys.exit(1)
    return create_client(url, key)

def get_existing(sb: Client, url: str):
    res = sb.table("regulations").select("*")\
        .eq("source", SOURCE_AUTHORITY).eq("url", url).limit(1).execute()
    return res.data[0] if res.data else None

def upsert_page(sb: Client, title: str, url: str, lang: str, category: str, content: str, content_hash: str, changed: bool):
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

    # Only bump last_updated when content actually changed
    if changed:
        payload["last_updated"] = now

    sb.table("regulations").upsert(
        payload,
        on_conflict="source,url"
    ).execute()

def scrape_one(sb: Client, title: str, url: str, category: str, lang: str, dry_run: bool):
    print(f"🔍 Fetching: {title} — {url}")
    html = fetch_html(url)
    text = clean_text(html)
    new_hash = sha256(text)
    print(f"ℹ️ Extracted length: {len(text)} chars")

    existing = get_existing(sb, url)

    if not existing:
        print("📥 No existing record found. Seeding…")
        if dry_run:
            print("🧪 [DRY-RUN] Would insert:", {"title": title, "url": url, "category": category})
            return
        upsert_page(sb, title, url, lang, category, text, new_hash, changed=True)
        print("✅ Inserted.")
        return

    old_hash = existing.get("content_hash")
    if old_hash == new_hash:
        print("✅ No change detected.")
        # still update last_fetched so you know it ran
        if not dry_run:
            upsert_page(sb, title, url, lang, category, existing["content"], existing["content_hash"], changed=False)
        return

    print("🔄 Change detected (hash differs after normalization).")
    if dry_run:
        # quick human hint (first differing 200 chars)
        old_text = existing.get("content", "")
        from itertools import zip_longest
        for a, b in zip_longest(old_text.splitlines(), text.splitlines(), fillvalue=""):
            if a != b:
                print("OLD:", a[:200])
                print("NEW:", b[:200])
                break
        print("🧪 [DRY-RUN] Would update row.")
        return

    upsert_page(sb, title, url, lang, category, text, new_hash, changed=True)
    print("✅ Updated row.")

def main(dry_run: bool = False, pause_sec: float = 1.0):
    sb = connect_supabase()
    for (title, url, category, lang) in SOURCES:
        try:
            scrape_one(sb, title, url, category, lang, dry_run)
        except Exception as e:
            print(f"❗ Error scraping {url}: {e}")
        time.sleep(pause_sec)  # polite pacing

if __name__ == "__main__":
    dry = len(sys.argv) > 1 and sys.argv[1].lower() in {"dry", "dry-run", "--dry-run"}
    main(dry_run=dry)
