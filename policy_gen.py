import os, json, hashlib
from datetime import datetime, timezone
from supabase import create_client, Client
from openai import OpenAI
from dotenv import load_dotenv
import sys
from llm_adapter import LLMAdapter

# Load environment variables
load_dotenv(dotenv_path=".env")  # Explicitly specify the path if needed

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ Missing SUPABASE_URL or SUPABASE_KEY env vars.")
    print(f"SUPABASE_URL: {SUPABASE_URL}")
    print(f"SUPABASE_KEY: {SUPABASE_KEY}")
    print(f"OPENAI_API_KEY: {OPENAI_API_KEY}")
    sys.exit(1)

# --- ENV ---
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")    # upgrade to gpt-4o when ready

sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
oai = OpenAI(api_key=OPENAI_API_KEY)
llm = LLMAdapter()

def get_client(company_name: str):
    res = sb.table("clients").select("*").eq("company_name", company_name).limit(1).execute()
    if not res.data:
        raise RuntimeError(f"Client not found: {company_name}")
    return res.data[0]

def get_fintrac_text():
    res = sb.table("regulations").select("*")\
        .eq("source","FINTRAC").eq("title","MSB Obligations").limit(1).execute()
    if not res.data:
        raise RuntimeError("FINTRAC MSB Obligations not found in regulations table.")
    return res.data[0]["content"]

def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def build_messages(client_row: dict, regs_text: str, language: str):
    system = (
        "You are a Canadian AML compliance writer for FINTRAC-regulated MSBs. "
        "Use only the provided FINTRAC text. If a detail is not present, write \"TBD\". "
        "Return STRICT JSON with keys:\n"
        "{\n"
        "  \"meta\": {\"jurisdiction\":\"Canada\",\"province\":\"<client province>\",\"language\":\"<en|fr>\"},\n"
        "  \"sections\": {\n"
        "    \"purpose_scope\":\"...\",\n"
        "    \"definitions\":\"...\",\n"
        "    \"risk_assessment\":\"...\",\n"
        "    \"kyc_cdd_edd\":\"...\",\n"
        "    \"recordkeeping\":\"...\",\n"
        "    \"reporting\":\"...\",\n"
        "    \"training\":\"...\",\n"
        "    \"governance\":\"...\",\n"
        "    \"monitoring_review\":\"...\",\n"
        "    \"province_specific\":\"...\"\n"
        "  },\n"
        "  \"citations\": [ {\"source\":\"FINTRAC\",\"title\":\"MSB Obligations\",\"excerpt\":\"...\",\"why\":\"...\"} ]\n"
        "}\n"
        "Do not include any text outside the JSON object."
    )
    user = (
        f"Client:\n"
        f"- Company: {client_row['company_name']}\n"
        f"- Province: {client_row['province']}\n"
        f"- Language: {language}\n\n"
        f"Authoritative text (FINTRAC for MSBs):\n{regs_text}\n\n"
        f"Write a practical, prescriptive AML policy for this client. Use short paragraphs and bullet points where helpful. "
        f"Cite excerpts where relevant."
    )
    return [{"role":"system","content":system},{"role":"user","content":user}]

def call_llm(messages):
    system_prompt = messages[0]["content"]
    user_prompt = messages[1]["content"]
    return llm.chat_json(system_prompt, user_prompt)

def to_markdown(sections_json: dict) -> str:
    s = sections_json["sections"]
    def sec(title, key):
        return f"# {title}\n\n{s.get(key,'TBD')}\n\n"
    md = (
        sec("1. Purpose & Scope", "purpose_scope") +
        sec("2. Definitions", "definitions") +
        sec("3. Risk Assessment", "risk_assessment") +
        sec("4. KYC / CDD / EDD", "kyc_cdd_edd") +
        sec("5. Recordkeeping", "recordkeeping") +
        sec("6. Reporting", "reporting") +
        sec("7. Training", "training") +
        sec("8. Governance", "governance") +
        sec("9. Monitoring & Review", "monitoring_review") +
        sec("10. Province-Specific Notes", "province_specific")
    )
    return md

def save_policy(client_id: str, language: str, reg_hash: str, sections_json: dict, policy_md: str):
    sb.table("policies").upsert({
        "client_id": client_id,
        "language": language,
        "regulation_source": "FINTRAC",
        "regulation_title": "MSB Obligations",
        "regulation_hash": reg_hash,
        "ai_model": AI_MODEL,
        "sections_json": sections_json,
        "policy_markdown": policy_md,
        "generated_at": datetime.now(timezone.utc).isoformat()
    }, on_conflict="client_id,regulation_source,regulation_title,regulation_hash").execute()

def generate_policy_for_client(company_name: str, preferred_language: str | None = None) -> str:
    client = get_client_by_name(company_name)
    language = preferred_language or client.get("language","en")
    prov = client.get("province","N/A")

    regs_text, regs_title = fetch_relevant_text_for_msb(lang=language)
    reg_hash = hashlib.sha256(regs_text.encode("utf-8")).hexdigest()

    user_prompt = f"""Client:
- Company: {client['company_name']}
- Province: {prov}
- Language: {language}

Relevant FINTRAC excerpts (MSB):
{regs_text}

Write a prescriptive AML policy for this client with concise, actionable language and cite where appropriate.
"""

    out = llm.chat_json(SYSTEM_PROMPT, user_prompt)

    policy_md = to_markdown(out)
    sb.table("policies").upsert({
        "client_id": client["id"],
        "language": language,
        "regulation_source": "FINTRAC",
        "regulation_title": regs_title,
        "regulation_hash": reg_hash,
        "ai_model": os.getenv("LLM_MODEL","gpt-4o-mini"),
        "sections_json": out,
        "policy_markdown": policy_md
    }, on_conflict="client_id,regulation_source,regulation_title,regulation_hash").execute()

    return policy_md

def fetch_relevant_text_for_msb(lang="en") -> tuple[str, str]:
    q = sb.table("regulations").select("title,category,content").eq("source", "FINTRAC").eq("lang", lang).execute()
    chunks = []
    for row in q.data or []:
        if (row.get("category") or "").strip() in RELEVANT_CATEGORIES_FOR_MSB:
            title = row.get("title", "(untitled)")
            content = row.get("content", "").strip()
            if content:
                chunks.append(f"### {title}\n{content}")
    if not chunks:
        raise RuntimeError("No relevant FINTRAC content found for MSB.")
    combined = "\n\n".join(chunks)
    return combined[:60000], "MSB Bundle"

if __name__ == "__main__":
    # Example:
    #   SUPABASE_URL=... SUPABASE_KEY=... OPENAI_API_KEY=... python policy_generator.py
    print("Generating policy for: MapleX Payments Inc.")
    doc = generate_policy_for_client("MapleX Payments Inc.")
    print(doc[:1200], "\n...\n[truncated]")
