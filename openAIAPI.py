import os, json, difflib
import requests
from bs4 import BeautifulSoup
from supabase import create_client, Client
from openai import OpenAI
import openai

SUPABASE_URL = "https://gnvtvcvdhajqymjbredf.supabase.co"
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_OR_SECRET"]  # set in your env
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]            # set in your env
FINTRAC_URL = "https://www.fintrac-canafe.gc.ca/msb-esm/obligations-eng"

sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
oai = OpenAI(api_key=OPENAI_API_KEY)
openai.api_key = OPENAI_API_KEY

def fetch_clean_text(url: str) -> str:
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    main = soup.find("main") or soup.body
    return main.get_text(separator="\n", strip=True)

def get_previous() -> str:
    res = sb.table("regulations").select("*").eq("source","FINTRAC").eq("title","MSB Obligations").execute()
    return (res.data[0]["content"] if res.data else "")

def store_new(content: str, ai_summary: dict | None):
    payload = {
        "title": "MSB Obligations",
        "source": "FINTRAC",
        "jurisdiction": "Canada",
        "category": "MSB",
        "content": content,
        "last_updated": "now()"
    }
    # upsert by (source,title)
    sb.rpc("upsert_regulation", {"p_source":"FINTRAC","p_title":"MSB Obligations", "p_payload":payload}).execute() \
        if "upsert_regulation" in [] else \
        sb.table("regulations").upsert(payload, on_conflict="title,source").execute()
    if ai_summary:
        sb.table("regulation_change_log").insert({
            "source":"FINTRAC","title":"MSB Obligations","summary_json":ai_summary
        }).execute()

def extract_changed_chunks(old: str, new: str, context_lines: int = 3, min_len: int = 200):
    """Heuristic: use difflib to pull only changed blocks with some context."""
    old_lines, new_lines = old.splitlines(), new.splitlines()
    sm = difflib.SequenceMatcher(None, old_lines, new_lines)
    chunks = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal": 
            continue
        # expand context
        i1c = max(0, i1 - context_lines); i2c = min(len(old_lines), i2 + context_lines)
        j1c = max(0, j1 - context_lines); j2c = min(len(new_lines), j2 + context_lines)
        old_chunk = "\n".join(old_lines[i1c:i2c]).strip()
        new_chunk = "\n".join(new_lines[j1c:j2c]).strip()
        if len(old_chunk) + len(new_chunk) >= min_len:  # skip trivial tiny edits
            chunks.append((old_chunk, new_chunk))
    # if nothing qualifies, fallback to small whole-text comparison (rare)
    return chunks or [(old[:4000], new[:4000])]

def ask_ai(old_chunk: str, new_chunk: str) -> dict:
    import openai
    openai.api_key = OPENAI_API_KEY

    system_msg = {
        "role": "system",
        "content": (
            "You are a Canadian financial compliance analyst specializing in FINTRAC AML obligations for MSBs. "
            "Return STRICT JSON only, following the exact schema with keys: "
            "is_meaningful_change, reason, categories, changes, regeneration_required."
        )
    }
    user_msg = {
        "role": "user",
        "content": f"OLD:\n{old_chunk}\n\nNEW:\n{new_chunk}\n\nContext: FINTRAC MSB obligations page. Evaluate only policy-relevant differences."
    }

    try:
        resp = openai.ChatCompletion.create(
            model="gpt-4",
            temperature=0.0,
            messages=[system_msg, user_msg]
        )
        txt = resp.choices[0].message.content
        return json.loads(txt)
    except json.JSONDecodeError:
        print("Failed to decode JSON response.")
        return {}
    except openai.error.OpenAIError as e:
        print(f"OpenAI API error: {e}")
        return {}

def evaluate_change(old_text: str, new_text: str) -> dict:
    results = []
    for old_chunk, new_chunk in extract_changed_chunks(old_text, new_text):
        results.append(ask_ai(old_chunk, new_chunk))
    # Aggregate: if any chunk is meaningful → meaningful
    meaningful = any(r.get("is_meaningful_change") for r in results)
    regen = any(r.get("regeneration_required") for r in results)
    return {
        "is_meaningful_change": meaningful,
        "regeneration_required": regen or meaningful,
        "chunks": results
    }

if __name__ == "__main__":
    new_text = fetch_clean_text(FINTRAC_URL)
    old_text = get_previous()
    if not old_text:
        # first run: store and exit
        store_new(new_text, ai_summary=None)
        print("Seeded initial FINTRAC content.")
    else:
        decision = evaluate_change(old_text, new_text)
        if decision["is_meaningful_change"]:
            store_new(new_text, ai_summary=decision)
            print("Meaningful change detected → stored + logged.")
            # here you can trigger your policy generator
        else:
            print("No meaningful change.")
