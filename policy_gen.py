from datetime import datetime, timezone
from dotenv import load_dotenv
from llm_adapter import LLMAdapter
from db_utils import sb, get_client_by_name as db_get_client_by_name, get_client_by_id as db_get_client_by_id
import os, json, hashlib, re, ast, codecs
from datetime import datetime, timezone, date

# token handling
try:
    import tiktoken
    _HAS_TIKTOKEN = True
except Exception:
    _HAS_TIKTOKEN = False

# load env
load_dotenv(dotenv_path=".env")

# --- ENV ---
# Prefer explicit model env vars used in this project (GEMINI_MODEL or LLM_MODEL).
# Default to Gemini Flash 2 if not set.
AI_MODEL = os.getenv("LLM_MODEL")
# instantiate adapter with the chosen model so adapter and generator are aligned
llm = LLMAdapter(model=AI_MODEL)

# categories used when assembling FINTRAC bundle
RELEVANT_CATEGORIES_FOR_MSB = {
    "MSB",
    "MSB Obligations",
    "Registration",
    "Guidance",
    "Interpretation",
    "Act",
}

def get_client(company_name: str):
    return db_get_client_by_name(company_name)

def get_client_by_name(company_name: str):
    """
    Fetches a client from the Supabase database by its company name.

    Args:
        company_name (str): The name of the company.

    Returns:
        dict: The client record, or None if not found.
    """
    res = sb.table("clients").select("*").eq("company_name", company_name).limit(1).execute()
    return res.data[0] if res.data else None

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
    "client_id": client["id"],
    "language": language,
    "regulation_source": regs_title,
    "regulation_title": regs_title,
    "regulation_hash": reg_hash,
    "ai_model": f"{PROVIDER}:{MODEL}",
    "sections_json": sections_json,
    "policy_markdown": policy_md,
    "generated_at": datetime.now(timezone.utc).isoformat()  # <-- important
}, on_conflict="client_id,regulation_source,regulation_title,regulation_hash").execute()

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

def _estimate_tokens(text: str, model: str | None = None) -> int:
    """
    Estimate token count for `text`. Use tiktoken when available, otherwise a conservative word-based heuristic.
    """
    if not text:
        return 0
    if _HAS_TIKTOKEN and model:
        try:
            enc = tiktoken.encoding_for_model(model)
            return len(enc.encode(text))
        except Exception:
            pass
    # fallback heuristic: assume ~0.75 words per token -> tokens ~= words / 0.75
    words = len(text.split())
    return max(1, int(words / 0.75))

def _prepare_prompt(client: dict, regs_text: str, language: str,
                    max_output_tokens: int = 800,
                    prompt_token_budget: int = 6000,
                    model_hint: str | None = None) -> tuple[str, int]:
    """
    Build user_prompt while ensuring we don't exceed token budgets.
    Returns (user_prompt, used_max_output_tokens).
    - max_output_tokens: desired output size (conservative default 800)
    - prompt_token_budget: total tokens allowed for the prompt (conservative default 6000)
    """
    # system + user preamble (without regs_text)
    preamble = (
        f"Client:\n- Company: {client['company_name']}\n- Province: {client.get('province','N/A')}\n- Language: {language}\n\n"
        "Relevant FINTRAC excerpts (MSB):\n"
    )

    # estimate tokens used by preamble and reserved output
    preamble_toks = _estimate_tokens(preamble, model_hint)
    reserved_output = max_output_tokens
    # available for regs_text
    avail_for_regs = max(0, prompt_token_budget - preamble_toks - reserved_output)

    # estimate regs tokens and truncate if necessary
    regs_toks = _estimate_tokens(regs_text, model_hint)
    if regs_toks > avail_for_regs:
        # try to use adapter truncate helper if available
        try:
            truncated = llm._truncate(regs_text, max_tokens=avail_for_regs, model=model_hint)
        except Exception:
            # fallback naive truncation by words
            words = regs_text.split()
            approx_words = max(10, int(avail_for_regs * 0.75))
            truncated = " ".join(words[:approx_words])
        regs_text = truncated

    user_prompt = preamble + regs_text + "\n\nWrite a prescriptive AML policy for this client with concise, actionable language and cite where appropriate."
    return user_prompt, max_output_tokens

def _extract_parts_text(s: str) -> str:
    """If the LLM returned a 'parts' wrapper (stringified), extract the inner text cleanly."""
    if not s or not isinstance(s, str):
        return s

    text = s

    # Normalize bold/markdown wrapping around the word "parts" (e.g. "**parts:**")
    text = re.sub(r'^[\s`]*\*{0,2}\s*parts\s*\*{0,2}\s*[:\*]*\s*', 'parts: ', text, flags=re.IGNORECASE)

    # 1) Try to find Python-style parts[...] repr and parse with ast.literal_eval
    m = re.search(r"parts\s*[:=]\s*(\[[\s\S]*\])", text, flags=re.IGNORECASE)
    if m:
        list_repr = m.group(1)
        try:
            obj = ast.literal_eval(list_repr)
            if isinstance(obj, list) and obj and isinstance(obj[0], dict):
                txt = obj[0].get("text") or obj[0].get("content")
                if isinstance(txt, str) and txt.strip():
                    return txt
        except Exception:
            # fall through to other heuristics
            pass

    # 2) Try JSON-like extraction: "candidates" / "content" / "parts" / "text"
    try:
        maybe_json = None
        # If the string looks like JSON anywhere, try to extract it and parse
        json_start = text.find('{')
        if json_start != -1:
            try:
                maybe_json = json.loads(text[json_start:])
            except Exception:
                # try to salvage by finding the first '[' that starts parts list
                pass
        if isinstance(maybe_json, dict):
            # navigate common Gemini response shapes
            try:
                cand = maybe_json.get("candidates") or maybe_json.get("outputs") or maybe_json.get("choices")
                if isinstance(cand, list) and cand:
                    # scan candidates for content.parts.text
                    for c in cand:
                        # nested shapes
                        content = c.get("content") if isinstance(c, dict) else None
                        if isinstance(content, dict):
                            parts = content.get("parts") or content.get("outputs") or []
                            if isinstance(parts, list) and parts:
                                p0 = parts[0]
                                if isinstance(p0, dict) and p0.get("text"):
                                    return p0.get("text")
                                if isinstance(p0, str):
                                    return p0
            except Exception:
                pass
    except Exception:
        pass

    # 3) Regex fallback: extract "text": "...." within the parts block (handles double quotes)
    m2 = re.search(r"""['"]?text['"]?\s*[:=]\s*["']([\s\S]+?)["']\s*(?:,|\])""", text, flags=re.DOTALL)
    if m2:
        return m2.group(1)

    # 4) If the string contains a visible markdown heading (## ), return from that heading
    m3 = re.search(r"(#{1,6}\s+[A-Za-z0-9].*)", text, flags=re.DOTALL)
    if m3:
        return text[m3.start():].strip()

    # nothing matched, return original
    return s

def _unescape_visible_escapes(text: str) -> str:
    """Convert literal escape sequences (\\n, \\r\\n, double-escaped) into real newlines."""
    if not text:
        return text
    t = str(text)
    # iterative unescape to handle double-escaped sequences
    for _ in range(4):
        prev = t
        t = t.replace("\\r\\n", "\r\n").replace("\\n", "\n").replace("\\t", "\t")
        t = t.replace("\\\\n", "\\n").replace("\\\\r\\\\n", "\\r\\n")
        try:
            dec = codecs.decode(t, "unicode_escape")
            if dec != t:
                t = dec
        except Exception:
            pass
        if t == prev:
            break
    # strip surrounding quotes if present
    if (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'")):
        t = t[1:-1]
    return t

def _fix_mojibake(text: str) -> str:
    """
    Try to repair double-encoded UTF-8 mojibake like "ÃÂ..." by applying latin1->utf8 decode iteratively.
    Safe no-op if text is fine.
    """
    if not text or not isinstance(text, str):
        return text
    s = text
    # only try if suspicious characters present
    if "Ã" not in s and "Â" not in s:
        return s
    for _ in range(3):
        try:
            s2 = s.encode("latin-1").decode("utf-8")
        except Exception:
            break
        if s2 == s:
            break
        s = s2
        # quick exit if fixed
        if "Ã" not in s and "Â" not in s:
            break
    return s

def generate_policy_for_client(company_name: str, preferred_language: str | None = None, custom_prompt: str | None = None) -> str:
    """
    Generate a policy. Accepts an optional custom_prompt containing {client} and {regs} placeholders.
    """
    client = get_client(company_name)
    if not client:
        raise RuntimeError(f"Client not found: {company_name}")

    language = preferred_language or client.get("language", "en")
    prov = client.get("province", "N/A")

    regs_text, regs_title = fetch_relevant_text_for_msb(lang=language)
    reg_hash = hashlib.sha256(regs_text.encode("utf-8")).hexdigest()

    if custom_prompt:
        client_summary = f"Company: {client['company_name']}\nProvince: {prov}\nLanguage: {language}"
        user_prompt = custom_prompt.replace("{client}", client_summary).replace("{regs}", regs_text)
    else:
        # use token-aware prompt builder
        prompt_tok_budget = int(os.getenv("PROMPT_TOKEN_BUDGET", "6000"))
        max_out = int(os.getenv("MAX_OUTPUT_TOKENS", "800"))
        user_prompt, max_out = _prepare_prompt(client, regs_text, language,
                                              max_output_tokens=max_out,
                                              prompt_token_budget=prompt_tok_budget,
                                              model_hint=os.getenv("LLM_MODEL", AI_MODEL))

    try:
        resp = llm.generate_text(user_prompt, max_output_tokens=int(os.getenv("MAX_OUTPUT_TOKENS", "800")), temperature=0.0)
        policy_text = llm.text_for(resp)
    except Exception as e:
        raise RuntimeError(f"LLM generation failed: {e}") from e

    # extract inner parts and unescape visible escapes so markdown renders correctly
    try:
        policy_text = _extract_parts_text(policy_text)
        policy_text = _unescape_visible_escapes(policy_text)
        # attempt to fix mojibake where bytes were double-decoded
        policy_text = _fix_mojibake(policy_text)
    except Exception:
        pass

    # Ensure we always store and return markdown text for UI
    policy_md = None
    sections_json = {}
    try:
        parsed = json.loads(policy_text)
        # convert structured JSON to readable markdown
        policy_md = _json_to_markdown(parsed)
        sections_json = parsed if isinstance(parsed, dict) else {}
    except Exception:
        # not JSON -> assume raw markdown/plain text
        policy_md = policy_text

    # fill placeholders (date, client) before persisting/displaying
    try:
        policy_md = _fill_placeholders(policy_md, client)
    except Exception:
        pass

    sb.table("policies").upsert({
        "client_id": client["id"],
        "language": language,
        "regulation_source": regs_title,
        "regulation_title": regs_title,
        "regulation_hash": reg_hash,
        "ai_model": os.getenv("LLM_PROVIDER", "gemini"),
        "sections_json": sections_json,
        "policy_markdown": policy_md,
        "generated_at": datetime.now(timezone.utc).isoformat()
    }, on_conflict="client_id,regulation_source,regulation_title,regulation_hash").execute()

    return policy_md

def generate_policy_with_gemini(prompt: str) -> str:
    resp = llm.generate_text(prompt, max_output_tokens=1200, temperature=0.0)
    return llm.text_for(resp)

# Gemini API integration
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
MODEL = "gemini-2.0-flash"  # adjust as available
URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"

def call_gemini(prompt: str, max_output_tokens: int = 512, temperature: float = 0.0, candidate_count: int = 1):
    """
    Correct Gemini generateContent call (v1beta). Uses generationConfig with camelCase keys.
    """
    if not GEMINI_KEY:
        raise RuntimeError("GEMINI_API_KEY not set")

    headers = {
        "Content-Type": "application/json",
        "X-goog-api-key": GEMINI_KEY
    }
    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": prompt}]}
        ],
        "generationConfig": {
            "temperature": float(temperature),
            "maxOutputTokens": int(max_output_tokens),
            "candidateCount": int(candidate_count)
        }
        # You can also add safetySettings here if you need them
        # "safetySettings": [...]
    }

    resp = requests.post(URL, headers=headers, json=payload, timeout=60)
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        raise RuntimeError(f"Gemini API request failed: {resp.status_code} - {resp.text}") from e

    data = resp.json()
    # Typical response shape:
    # { "candidates":[{"content":{"parts":[{"text":"..."}]}}], ... }
    text = ""
    try:
        text = data["candidates"][0]["content"]["parts"][0].get("text", "")
    except Exception:
        # last resort: show raw json
        text = json.dumps(data)
    return text, data

if __name__ == "__main__":
    import os, traceback
    print("🔎 policy_generator.py entrypoint", flush=True)

    # Sanity: show provider/model + critical envs
    provider = os.getenv("LLM_PROVIDER", "gemini")
    model = os.getenv("LLM_MODEL", "gemini-1.5-flash")
    sb_url = os.getenv("SUPABASE_URL")
    have_key = bool(os.getenv("GEMINI_API_KEY") or os.getenv("OPENAI_API_KEY"))
    print(f"LLM: {provider}:{model} | SUPABASE_URL set: {bool(sb_url)} | LLM key present: {have_key}", flush=True)

    try:
        CLIENT_NAME = os.getenv("TEST_CLIENT", "MapleX Payments Inc.")
        print(f"⚙️  Generating policy for: {CLIENT_NAME}", flush=True)
        doc = generate_policy_for_client(CLIENT_NAME)
        print(f"✅ Generated policy length: {len(doc)} characters", flush=True)
        print(doc[:800] + "\n...\n[truncated]", flush=True)
    except Exception as e:
        print("❌ Error in generate_policy_for_client:", e, flush=True)
        traceback.print_exc()
