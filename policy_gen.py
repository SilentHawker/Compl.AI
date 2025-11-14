from datetime import datetime, timezone
from dotenv import load_dotenv
from llm_adapter import LLMAdapter
from db_utils import sb, get_client_by_name as db_get_client_by_name
import os, json, hashlib, re, ast, codecs
from typing import Optional, Tuple

load_dotenv(dotenv_path=".env")

AI_MODEL = os.getenv("LLM_MODEL")
llm = LLMAdapter(model=AI_MODEL)

RELEVANT_CATEGORIES_FOR_MSB = {
    "MSB",
    "MSB Obligations",
    "Registration",
    "Guidance",
    "Interpretation",
    "Act",
}

MASTER_POLICY_PROMPT = """Role / Persona
 You are a senior Canadian AML/ATF and regulatory compliance specialist with deep knowledge of the PCMLTFA, its Regulations, and FINTRAC guidance for Money Services Businesses (MSBs). You know the reporting and record-keeping requirements for LCTR, LVCTR, EFTR, 24-hour rule, travel rule, virtual currency, ministerial directives, and the KYC/ID methods (photo ID, credit file, dual-process, reliance, agent/mandatary, SDD). You also understand multi-service MSBs (remittance, FX, VC, negotiable instruments, cheque cashing, crowdfunding, transport services).

Input Documents
“Client policy pre-production questionnaire.docx” — use the client’s actual answers to determine which sections and subsections are in scope. Do not assume “Yes” unless the user says so.

“Policy Creation Decision Tree_MSB.docx” — this is the master logic that says: if the questionnaire answer is “Yes”, then the corresponding bold section/subsection must appear in the policy.

(Optional) Client’s existing AML/Compliance Policy — if provided, compare to the decision tree + questionnaire.

Goal
 Produce a complete policy coverage map and gap analysis for a Canadian MSB, and then draft the fully expanded AML/ATF Policy that reflects all “Yes” branches in the decision tree, aligned to FINTRAC guidance.

Note: {client} and {regs} placeholders may be substituted by the caller.
"""

# ----------------- Helpers -----------------
def get_client(company_name: str) -> Optional[dict]:
    res = db_get_client_by_name(company_name)
    return res

def fetch_relevant_text_for_msb(lang: str = "en") -> Tuple[str, str]:
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

def _estimate_tokens(text: str, model: Optional[str] = None) -> int:
    try:
        import tiktoken
        enc = tiktoken.encoding_for_model(model) if model else tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        words = len(text.split())
        return max(1, int(words / 0.75))

def _prepare_prompt(client: dict, regs_text: str, language: str,
                    max_output_tokens: int = 800,
                    prompt_token_budget: int = 6000,
                    model_hint: Optional[str] = None) -> Tuple[str, int]:
    preamble = (
        f"Client:\n- Company: {client['company_name']}\n- Province: {client.get('province','N/A')}\n- Language: {language}\n\n"
        "Relevant FINTRAC excerpts (MSB):\n"
    )
    preamble_toks = _estimate_tokens(preamble, model_hint)
    reserved_output = max_output_tokens
    avail_for_regs = max(0, prompt_token_budget - preamble_toks - reserved_output)
    regs_toks = _estimate_tokens(regs_text, model_hint)
    if regs_toks > avail_for_regs:
        try:
            regs_text = llm._truncate(regs_text, max_tokens=avail_for_regs, model=model_hint)
        except Exception:
            words = regs_text.split()
            approx_words = max(10, int(avail_for_regs * 0.75))
            regs_text = " ".join(words[:approx_words])
    user_prompt = preamble + regs_text + "\n\nWrite a prescriptive AML policy for this client with concise, actionable language and cite where appropriate."
    return user_prompt, max_output_tokens

def _extract_parts_text(s: str) -> str:
    if not s or not isinstance(s, str):
        return s
    text = s
    text = re.sub(r'^[\s`]*\*{0,2}\s*parts\s*\*{0,2}\s*[:\*]*\s*', 'parts: ', text, flags=re.IGNORECASE)
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
            pass
    m2 = re.search(r"""['"]?text['"]?\s*[:=]\s*["']([\s\S]+?)["']\s*(?:,|\])""", text, flags=re.DOTALL)
    if m2:
        return m2.group(1)
    m3 = re.search(r"(#{1,6}\s+[A-Za-z0-9].*)", text, flags=re.DOTALL)
    if m3:
        return text[m3.start():].strip()
    return s

def _unescape_visible_escapes(text: str) -> str:
    if not text:
        return text
    t = str(text)
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
    if (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'")):
        t = t[1:-1]
    return t

def _fix_mojibake(text: str) -> str:
    if not text or not isinstance(text, str):
        return text
    s = text
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
        if "Ã" not in s and "Â" not in s:
            break
    return s

def _json_to_markdown(obj) -> str:
    try:
        if isinstance(obj, dict) and "sections" in obj:
            parts = []
            sec = obj.get("sections", {})
            for k, v in sec.items():
                title = k.replace("_", " ").title()
                parts.append(f"## {title}\n\n{v}\n")
            return "\n".join(parts).strip()
    except Exception:
        pass
    try:
        return json.dumps(obj, indent=2, ensure_ascii=False)
    except Exception:
        return str(obj)

def _fill_placeholders(md: str, client: dict) -> str:
    if not md:
        return md
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    name = client.get("company_name") or client.get("name") or ""
    md = md.replace("[Date]", today).replace("[DATE]", today).replace("{date}", today).replace("{{date}}", today)
    md = md.replace("{client}", name).replace("[Company]", name).replace("[COMPANY]", name).replace("{company}", name)
    return md

# ----------------- Main export -----------------
def generate_policy_for_client(company_name: str, preferred_language: Optional[str] = None, custom_prompt: Optional[str] = None) -> str:
    """
    Generate a policy markdown string for `company_name`.
    Does NOT persist to DB — persistence should be handled by the caller.
    """
    client = get_client(company_name)
    if not client:
        raise RuntimeError(f"Client not found: {company_name}")

    language = preferred_language or client.get("language", "en")
    prov = client.get("province", "N/A")
    regs_text, regs_title = fetch_relevant_text_for_msb(lang=language)
    reg_hash = hashlib.sha256(regs_text.encode("utf-8")).hexdigest()

    client_summary = f"Company: {client['company_name']}\nProvince: {prov}\nLanguage: {language}"
    master_filled = MASTER_POLICY_PROMPT.replace("{client}", client_summary).replace("{regs}", regs_title)

    if custom_prompt:
        try:
            custom_filled = custom_prompt.replace("{client}", client_summary).replace("{regs}", regs_text)
        except Exception:
            custom_filled = custom_prompt
        user_prompt = master_filled + "\n\n" + custom_filled
    else:
        prompt_tok_budget = int(os.getenv("PROMPT_TOKEN_BUDGET", "6000"))
        max_out = int(os.getenv("MAX_OUTPUT_TOKENS", "800"))
        body_prompt, max_out = _prepare_prompt(client, regs_text, language,
                                              max_output_tokens=max_out,
                                              prompt_token_budget=prompt_tok_budget,
                                              model_hint=os.getenv("LLM_MODEL", AI_MODEL))
        user_prompt = master_filled + "\n\n" + body_prompt

    resp = llm.generate_text(user_prompt, max_output_tokens=int(os.getenv("MAX_OUTPUT_TOKENS", "800")), temperature=0.0)
    policy_text = llm.text_for(resp) if hasattr(llm, "text_for") else str(resp)

    try:
        policy_text = _extract_parts_text(policy_text)
        policy_text = _unescape_visible_escapes(policy_text)
        policy_text = _fix_mojibake(policy_text)
    except Exception:
        pass

    policy_md = None
    try:
        parsed = json.loads(policy_text)
        policy_md = _json_to_markdown(parsed)
    except Exception:
        policy_md = policy_text

    try:
        policy_md = _fill_placeholders(policy_md, client)
    except Exception:
        pass

    return policy_md

def generate_gap_suggestions(company_name: str,
                             existing_policy_md: str,
                             preferred_language: Optional[str] = None,
                             max_output_tokens: int = 600) -> list:
    """
    Compare `existing_policy_md` to relevant FINTRAC excerpts for the client and return
    a list of suggested modular edits where the policy is Missing or Partially Present.
    Each suggestion is a dict with:
      - id: short unique id
      - section: suggested section/subsection title
      - trigger: which regulatory/topic made it in-scope
      - fintrac_refs: list of short citations/URLs
      - current_status: "Missing" | "Partial" | "Present"
      - suggestion_md: markdown snippet to insert/replace
      - insertion_point: heading text or "end_of_section" (frontend can use to place patch)
      - rationale: short explanation / remediation text
    """
    client = get_client(company_name)
    if not client:
        raise RuntimeError(f"Client not found: {company_name}")

    language = preferred_language or client.get("language", "en")
    regs_text, regs_title = fetch_relevant_text_for_msb(lang=language)

    # build compact comparison prompt
    preamble = (
        f"Client:\n- Company: {client['company_name']}\n- Province: {client.get('province','N/A')}\n- Language: {language}\n\n"
        f"Uploaded policy (brief):\n{(existing_policy_md[:4000] + '...') if existing_policy_md else '[EMPTY]'}\n\n"
        "Relevant FINTRAC excerpts (MSB):\n"
    )

    # try to respect token budget / truncate regs if needed
    body_budget = max_output_tokens
    prompt_body = preamble + regs_text[:40000]  # conservative truncate
    prompt_instructions = (
        "\n\nTask: Compare the uploaded policy to the provided FINTRAC excerpts. "
        "For each required obligation or section that is Missing or Partially Present in the uploaded policy, "
        "produce one suggestion object. Output a JSON array only. Each object must include the fields: "
        "id, section, trigger, fintrac_refs (array of short citations or URLs), current_status (Missing|Partial|Present), "
        "suggestion_md (a concise markdown snippet to insert), insertion_point (heading text or 'end_of_section'), and rationale (1-2 sentences). "
        "Keep suggestion_md short (1-6 paragraphs). Prefer conservative compliance language. "
        "Do NOT include the entire full policy — only modular suggestion snippets. "
        "If a requirement is Not Applicable, do not include it."
    )

    user_prompt = prompt_body + "\n\n" + prompt_instructions

    try:
        resp = llm.generate_text(user_prompt, max_output_tokens=max_output_tokens, temperature=0.0)
        text = llm.text_for(resp) if hasattr(llm, "text_for") else str(resp)
    except Exception as e:
        raise RuntimeError(f"LLM generation failed: {e}") from e

    # attempt to parse JSON from the model output
    suggestions = []
    try:
        # try direct JSON
        parsed = json.loads(text)
        if isinstance(parsed, list):
            suggestions = parsed
        elif isinstance(parsed, dict) and parsed.get("suggestions"):
            suggestions = parsed["suggestions"]
    except Exception:
        # try to extract first JSON array present
        m = re.search(r"(\[\s*{[\s\S]*}\s*\])", text)
        if m:
            try:
                suggestions = json.loads(m.group(1))
            except Exception:
                suggestions = []
        else:
            # fallback: heuristic split by headings — produce minimal suggestion indicating failure to parse
            suggestions = [{
                "id": "suggestion_parse_error",
                "section": "Parsing / Analysis",
                "trigger": "internal",
                "fintrac_refs": [],
                "current_status": "Partial",
                "suggestion_md": "LLM returned an unexpected format. Please re-run the analysis or check the logs.",
                "insertion_point": "preface",
                "rationale": "Unable to parse LLM JSON output; this indicates the model did not return structured JSON."
            }]

    # Normalize each suggestion to expected keys and small sanity checks
    normalized = []
    for i, s in enumerate(suggestions):
        try:
            sid = s.get("id") if isinstance(s, dict) else None
            if not sid:
                sid = f"sugg_{i}"
            normalized.append({
                "id": sid,
                "section": (s.get("section") if isinstance(s, dict) else s) or "Unspecified",
                "trigger": (s.get("trigger") if isinstance(s, dict) else "") or "",
                "fintrac_refs": s.get("fintrac_refs") if isinstance(s, dict) and isinstance(s.get("fintrac_refs"), list) else [],
                "current_status": (s.get("current_status") if isinstance(s, dict) else "Missing") or "Missing",
                "suggestion_md": (s.get("suggestion_md") if isinstance(s, dict) else "") or "",
                "insertion_point": (s.get("insertion_point") if isinstance(s, dict) else "end_of_section") or "end_of_section",
                "rationale": (s.get("rationale") if isinstance(s, dict) else "") or ""
            })
        except Exception:
            continue

    return normalized
