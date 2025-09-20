# app.py
import os, time, uuid
from datetime import datetime
from dotenv import load_dotenv
from io import BytesIO
from html2docx import html2docx
import markdown as mdlib
import json
import streamlit as st
import bcrypt
import re
import ast
import codecs
from llm_adapter import LLMAdapter

# Default hard-coded prompt shown in the advanced prompt box when no previous prompt is present
DEFAULT_POLICY_PROMPT = """Write a concise, prescriptive AML policy for {client} using the relevant regulatory excerpts provided in {regs}.
Produce well-structured Markdown with a title, effective date, summary, and numbered sections (Purpose, Scope, KYC, Monitoring, Reporting, Record Keeping, Travel Rule, etc.).
Be actionable and cite regulations where appropriate. Keep language clear and suitable for a Canadian MSB compliance program."""

from db_utils import (
    sb,
    list_clients as db_list_clients,
    list_policies as db_list_policies,
    get_client_by_token as db_get_client_by_token,
    get_client_by_username as db_get_client_by_username,
    get_client_by_id as db_get_client_by_id,
    get_client_by_name as db_get_client_by_name,
    list_sources as db_list_sources,
    list_registrations_for_versions as db_list_regs_for_versions,
    list_versions as db_list_versions,
    get_version_content_by_no as db_get_version_content_by_no,
    get_policies_by_client as db_get_policies_by_client,
)

def list_clients(): return db_list_clients()
def list_policies(client_id=None): return db_list_policies(client_id)
def get_client_by_token(tok): return db_get_client_by_token(tok)
def get_client_by_username(username): return db_get_client_by_username(username)
def get_client_by_id(client_id): return db_get_client_by_id(client_id)
def get_client_by_name(company_name): return db_get_client_by_name(company_name)
def list_sources(): return db_list_sources()
def list_registrations_for_versions(): return db_list_regs_for_versions()
def list_versions(regulation_id): return db_list_versions(regulation_id)
def get_version_content_by_no(regulation_id, version_no): return db_get_version_content_by_no(regulation_id, version_no)
def get_policies_by_client(client_id): return db_get_policies_by_client(client_id)

def _rerun():
    if hasattr(st, "rerun"):
        st.rerun()
    elif hasattr(st, "experimental_rerun"):
        st.experimental_rerun()

def _get_qp() -> dict:
    if hasattr(st, "query_params"):
        # st.query_params behaves like a MutableMapping
        return dict(st.query_params)
    elif hasattr(st, "experimental_get_query_params"):
        return st.experimental_get_query_params()
    return {}

def _set_qp(params: dict):
    if hasattr(st, "query_params"):
        st.query_params.clear()
        for k, v in params.items():
            st.query_params[k] = v
    elif hasattr(st, "experimental_set_query_params"):
        st.experimental_set_query_params(**params)

# ------------------ ENV ------------------
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Credentials (override via env if you want)
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")
CLIENT_USER = os.getenv("CLIENT_USER", "test")
CLIENT_PASS = os.getenv("CLIENT_PASS", "test123")

# Where client portal links should point (same app is fine)
PORTAL_BASE = os.getenv("CLIENT_PORTAL_BASE_URL", "http://localhost:8501")

st.set_page_config(page_title="ComplianceAI", layout="wide")

# Try to import your real generator; otherwise stub so UI still runs
try:
    from policy_gen import generate_policy_for_client
    HAVE_GENERATOR = True
except Exception:
    HAVE_GENERATOR = False
    def generate_policy_for_client(company_name: str, preferred_language: str | None = None) -> str:
        return f"# AML Policy for {company_name}\n\n(Connect policy_generator.py to enable real generation.)\n"

# ------------------ HELPERS ------------------
def ts(s):
    if not s: return "—"
    try:
        return datetime.fromisoformat(s.replace("Z","")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return s

def _dict_to_markdown(obj) -> str:
    """
    Convert various inputs to readable Markdown text (no fenced code blocks).
    - If obj is a JSON string, parse and render as markdown.
    - If obj is dict/list with common policy shape, render headings/sections.
    - Otherwise return plain text or pretty JSON without code fences.
    """
    def _json_to_md(j) -> str:
        parts = []
        if isinstance(j, dict):
            title = j.get("title") or j.get("name") or ""
            if title:
                parts.append(f"# {title}\n")
            summary = j.get("summary") or j.get("desc") or j.get("description") or ""
            if summary:
                parts.append(f"**Summary:** {summary}\n")
            sections = j.get("sections") or j.get("items") or []
            if isinstance(sections, list) and sections:
                for s in sections:
                    if isinstance(s, dict):
                        h = s.get("heading") or s.get("title") or ""
                        if h:
                            parts.append(f"## {h}\n")
                        req = s.get("requirement") or s.get("text") or s.get("body") or ""
                        if req:
                            parts.append(f"{req}\n")
                        cit = s.get("citation") or s.get("citations") or ""
                        if cit:
                            parts.append(f"**Citation:** {cit}\n")
                        parts.append("\n")
                    else:
                        parts.append(f"- {s}")
            # fallback to key/value pairs if no sections
            if not parts:
                for k, v in j.items():
                    parts.append(f"**{k}:** {v}")
            return "\n".join(p for p in parts).strip()
        if isinstance(j, list):
            out = []
            for i, item in enumerate(j, start=1):
                out.append(f"## Item {i}")
                if isinstance(item, (dict, list)):
                    out.append(_json_to_md(item))
                else:
                    out.append(str(item))
            return "\n\n".join(out).strip()
        return str(j)

    try:
        # If string that looks like JSON, try parsing
        if isinstance(obj, str):
            s = obj.strip()
            # remove surrounding triple-fence if present
            if s.startswith("```") and s.endswith("```"):
                s = "\n".join(s.splitlines()[1:-1]).strip()
            if s.startswith("{") or s.startswith("["):
                try:
                    parsed = json.loads(s)
                    return _json_to_md(parsed)
                except Exception:
                    return s  # leave raw text if parse fails
            return s

        if isinstance(obj, (dict, list)):
            return _json_to_md(obj)

        return str(obj)
    except Exception:
        return str(obj)

def _fix_mojibake(text: str) -> str:
    """
    Best-effort repair for double-encoded UTF-8 mojibake (e.g. sequences like "ÃÂ...").
    Applies latin1->utf8 decoding iteratively; safe no-op if not needed.
    """
    if not text or not isinstance(text, str):
        return text
    if "Ã" not in text and "Â" not in text:
        return text
    s = text
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

# ---- Password check helpers ----
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def check_password_bcrypt(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False

def verify_password(password: str, stored: str) -> bool:
    """
    Returns True if password matches stored credential.
    - Supports bcrypt (stored starts with $2a/$2b/$2y) and plaintext (MVP).
    """
    if not stored:
        return False
    if stored.startswith("$2a$") or stored.startswith("$2b$") or stored.startswith("$2y$"):
        return check_password_bcrypt(password, stored)
    return password == stored

def add_client(name, prov, lang):
    sb.table("clients").insert({"company_name": name, "province": prov, "language": lang}).execute()

def rotate_token_python(client_id):
    new_tok = str(uuid.uuid4())
    sb.table("clients").update({"portal_token": new_tok}).eq("id", client_id).execute()
    return new_tok

# set_client_portal_creds now hashes password before storing
def set_client_portal_creds(client_id: str, user: str, pwd: str, enabled: bool = True):
    hashed = bcrypt.hashpw(pwd.encode("utf-8"), bcrypt.gensalt()).decode("utf-8") if pwd else (sb.table("clients").select("portal_pass").eq("id", client_id).limit(1).execute().data[0].get("portal_pass") if sb.table("clients").select("portal_pass").eq("id", client_id).limit(1).execute().data else "")
    sb.table("clients").update({
        "portal_user": user.strip(),
        "portal_pass": hashed,
        "portal_enabled": enabled
    }).eq("id", client_id).execute()

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

def md_to_docx_bytes(md_text: str, title: str = "AML Policy") -> bytes:
    """
    Convert Markdown -> HTML -> .docx bytes using html2docx.
    Robust to different html2docx versions: try calling with title, handle bytes/BytesIO/filename returns.
    """
    html = mdlib.markdown(md_text, extensions=["tables", "fenced_code", "toc"])

    # Try calling html2docx with title first (some versions require it)
    docx_result = None
    try:
        docx_result = html2docx(html, title=title)
    except TypeError:
        # older/newer signature differences — try without title
        docx_result = html2docx(html)

    # If html2docx returned a filename, read bytes
    if isinstance(docx_result, str):
        with open(docx_result, "rb") as f:
            return f.read()

    # If returned bytes or bytearray, return directly
    if isinstance(docx_result, (bytes, bytearray)):
        return bytes(docx_result)

    # If returned a file-like object (BytesIO), read it
    try:
        # Some versions may return a BytesIO or similar
        docx_result.seek(0)
        data = docx_result.read()
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
    except Exception:
        pass

    raise RuntimeError("html2docx did not return a valid .docx bytes object")

# ------------------ AUTH ------------------
def show_login():
     st.title("ComplianceAI — Sign in")
     with st.form("login_form"):
         u = st.text_input("Username", value="", autocomplete="username")
         p = st.text_input("Password", value="", type="password", autocomplete="current-password")
         role = st.selectbox("Login as", ["client", "admin"], index=0)
         submitted = st.form_submit_button("Log in")
         if submitted:
             if role == "admin" and u == ADMIN_USER and p == ADMIN_PASS:
                 st.session_state["authed"] = True
                 st.session_state["role"] = "admin"
                 st.success("Logged in as admin.")
                 time.sleep(0.3)
                 _rerun()
             elif role == "client":
                client = get_client_by_username(u)
                st.write(f"Debug: Client fetched: {client}")
                if client and client.get("portal_enabled", True) and verify_password(p, client.get("portal_pass", "") or ""):
                     st.session_state["authed"] = True
                     st.session_state["role"] = "client"
                     st.session_state["client_id"] = client["id"]  # <-- bind to a specific client
                     st.success(f"Logged in as client: {client['company_name']}")
                     _rerun()
                else:
                     st.error("Invalid client credentials or portal disabled")
             else:
                 st.error("Invalid credentials")

def ensure_auth():
    if "authed" not in st.session_state:
        st.session_state["authed"] = False
        st.session_state["role"] = None
    if not st.session_state["authed"]:
        show_login()
        st.stop()

def logout_button():
    if st.button("Log out", type="secondary"):
        st.session_state.clear()
        st.success("Logged out.")
        time.sleep(0.3)
        _rerun()

# ------------------ ADMIN UI ------------------
def admin_ui():
    st.title("ComplianceAI — Admin Dashboard")
    logout_button()

    tab1, tab2, tab3, tab4 = st.tabs(["👥 Customers", "🧾 Policies", "📚 Sources", "🗂 Versions"])

    # ---------- Customers ----------
    with tab1:
        st.subheader("Add a customer")
        with st.form("add_client"):
            name = st.text_input("Company name", "")
            prov = st.selectbox("Province", ["QC", "ON", "BC", "AB", "MB", "SK", "NB", "NS", "NL", "PE", "YT", "NT", "NU"], index=1)
            lang = st.selectbox("Language", ["en", "fr"], index=0)
            submitted = st.form_submit_button("Save")
            if submitted:
                if not name.strip():
                    st.error("Company name required.")
                else:
                    add_client(name.strip(), prov, lang)
                    st.success(f"Saved: {name}")
                    time.sleep(0.4)
                    _rerun()

        st.markdown("### All customers")
        clients = list_clients()
        if not clients:
            st.info("No customers yet.")
        else:
            for c in clients:
                with st.container(border=True):
                    st.subheader(c["company_name"])
                    st.caption(f"{c['province']} • {c['language']} • Added {ts(c['created_at'])}")
                    col1, col2, col3 = st.columns([2, 2, 2])

                    with col1:
                        enabled = "✅ enabled" if c.get("portal_enabled") else "❌ disabled"
                        st.text(f"Client portal: {enabled}")
                        link = f"{PORTAL_BASE}/?token={c.get('portal_token', '')}"
                        st.code(link, language="text")
                        if st.button(f"Rotate link", key="rot_" + c["id"]):
                            new_tok = rotate_token_python(c["id"])
                            st.success("Link rotated.")
                            st.code(f"{PORTAL_BASE}/?token={new_tok}", language="text")

                    with col2:
                        # Advanced prompt input (per-client) - prefill with last prompt (session first, then DB)
                        session_key = f"last_prompt_{c['id']}"
                        prefill = st.session_state.get(session_key, c.get("last_prompt") or DEFAULT_POLICY_PROMPT)
                        adv_prompt = st.text_area("Advanced prompt (optional)", value=prefill, key=f"advp_{c['id']}", height=160)
                        clicked = st.button(f"Generate policy", key="gen_" + c["id"])
                        if clicked:
                            if HAVE_GENERATOR:
                                with st.spinner("Generating…"):
                                    # pass custom prompt through to generator
                                    md = generate_policy_for_client(c["company_name"], preferred_language=c.get("language"), custom_prompt=(adv_prompt or None))
                                st.session_state[f"admin_last_md_{c['id']}"] = md
                                st.session_state[f"admin_last_fn_{c['id']}"] = f"{c['company_name']}_AML_Policy.md"
                                # persist last prompt in session and try to persist on the client row (best-effort)
                                st.session_state[f"last_prompt_{c['id']}"] = adv_prompt or ""
                                try:
                                    sb.table("clients").update({"last_prompt": adv_prompt or ""}).eq("id", c["id"]).execute()
                                except Exception:
                                    pass
                                st.success("Policy generated. Scroll to download below.")
                            else:
                                st.warning("Connect policy_generator.py to enable real generation.")

                        # Show last result for this card (if any) — fetch once and guard all uses
                        admin_key = f"admin_last_md_{c['id']}"
                        amd_raw = st.session_state.get(admin_key)
                        if amd_raw:
                            # normalize all incoming shapes into readable markdown text
                            amd = normalize_policy_text(amd_raw)
                            with st.expander("Latest generated policy"):
                                st.markdown(amd)
                            fname_base = st.session_state.get("last_policy_filename", "AML_Policy")
                            st.download_button("⬇️ Download latest (Markdown)", amd or "", file_name=f"{fname_base}.md", mime="text/markdown")
                            try:
                                docx_bytes = md_to_docx_bytes(amd or "")
                                st.download_button("⬇️ Download latest (Word .docx)", docx_bytes, file_name=f"{fname_base}.docx", mime=DOCX_MIME)
                            except Exception as e:
                                st.caption(f"Could not build .docx: {e}")

    # ---------- Policies ----------
    with tab2:
        st.subheader("All policies")
        policies = list_policies()
        if not policies:
            st.info("No policies found.")
        else:
            for p in policies:
                with st.container(border=True):
                    st.subheader(f"Policy {p.get('id','?')}")
                    st.caption(f"v{p.get('version_no','?')} • {ts(p.get('created_at') or p.get('generated_at'))}")
                    # policy text column is "policy_markdown" in the DB
                    md = normalize_policy_text(p.get("policy_markdown") or p.get("policy_md"))
                    st.markdown(md or "")
                    st.download_button("⬇️ Download policy", md or "", file_name=f"Policy_{p['id']}.md", mime="text/markdown")

    # ---------- Sources ----------
    with tab3:
        st.subheader("All sources")
        sources = list_sources()
        if not sources:
            st.info("No sources found.")
        else:
            for s in sources:
                with st.container(border=True):
                    # Defensive access: handle differing DB column names / missing fields
                    src_name = s.get("source_name") or s.get("name") or s.get("title") or "(unnamed source)"
                    src_type = s.get("source_type") or s.get("type") or "unknown"
                    created = ts(s.get("created_at") or s.get("created") or s.get("added_at"))
                    src_url = s.get("source_url") or s.get("url") or s.get("source_uri") or ""

                    st.subheader(src_name)
                    st.caption(f"Type: {src_type} • Added {created}")
                    if src_url:
                        st.write(src_url)
                        safe_fn = "".join(c for c in src_name if c.isalnum() or c in (" ", "-", "_")).strip()[:120] or "source"
                        try:
                            st.download_button("⬇️ Download source", src_url, file_name=f"{safe_fn}", mime="application/octet-stream")
                        except Exception:
                            # fallback: expose URL only if download fails
                            st.write("(Download unavailable; URL shown above)")
                    else:
                        st.write("(no URL available)")

    # ---------- Versions ----------
    with tab4:
        st.subheader("All regulation versions")
        try:
            regs = list_versions()
        except TypeError:
            regs = list_versions(None)
        if not regs:
            st.info("No regulations found.")
        else:
            for r in regs:
                with st.container(border=True):
                    st.subheader(f"Regulation {r['id']}")
                    st.caption(f"v{r['version_no']} • {ts(r['created_at'])}")
                    content = get_version_content_by_no(r['id'], r['version_no'])
                    content = normalize_policy_text(content)
                    st.markdown(content or "")
                    st.download_button("⬇️ Download regulation", content or "", file_name=f"Regulation_{r['id']}_v{r['version_no']}.md", mime="text/markdown")

# ------------------ CLIENT PORTAL ------------------
def client_portal_ui():
    st.title("ComplianceAI — Client Portal")
    logout_button()

    client_id = st.session_state.get("client_id")
    client = get_client_by_id(client_id)
    if not client:
        st.error("Client not found")
        return

    st.subheader(f"Welcome, {client['company_name']}!")
    st.caption(f"Last login: {ts(client.get('last_login'))}")

    # --- Policy management ---
    st.markdown("### Your policies")
    policies = list_policies(client_id)
    if not policies:
        st.info("No policies found. Please contact your administrator.")
    else:
        for p in policies:
            with st.container(border=True):
                st.subheader(f"Policy {p.get('id','?')}")
                st.caption(f"v{p.get('version_no','?')} • {ts(p.get('created_at') or p.get('generated_at'))}")
                md = normalize_policy_text(p.get("policy_markdown") or p.get("policy_md"))
                st.markdown(md or "")
                st.download_button("⬇️ Download policy", md or "", file_name=f"Policy_{p['id']}.md", mime="text/markdown")

    # --- Registration management ---
    st.markdown("### Your registrations")
    regs = list_registrations_for_versions()
    if not regs:
        st.info("No registrations found. Please contact your administrator.")
    else:
        for r in regs:
            with st.container(border=True):
                st.subheader(f"Regulation {r['id']}")
                st.caption(f"v{r['version_no']} • {ts(r['created_at'])}")
                content = get_version_content_by_no(r['id'], r['version_no'])
                content = normalize_policy_text(content)
                st.markdown(content or "")
                st.download_button("⬇️ Download regulation", content or "", file_name=f"Regulation_{r['id']}_v{r['version_no']}.md", mime="text/markdown")

    # --- Profile management ---
    st.markdown("### Your profile")
    with st.form("profile_form"):
        company_name = st.text_input("Company name", client["company_name"])
        province = st.selectbox("Province", ["QC", "ON", "BC", "AB", "MB", "SK", "NB", "NS", "NL", "PE", "YT", "NT", "NU"], index=["QC", "ON", "BC", "AB", "MB", "SK", "NB", "NS", "NL", "PE", "YT", "NT", "NU"].index(client["province"]))
        language = st.selectbox("Language", ["en", "fr"], index=0 if client["language"] == "en" else 1)
        password = st.text_input("New password", "", type="password")
        password_confirm = st.text_input("Confirm new password", "", type="password")
        submitted = st.form_submit_button("Update profile")
        if submitted:
            if password != password_confirm:
                st.error("Passwords do not match")
            else:
                # Update client info
                sb.table("clients").update({
                    "company_name": company_name,
                    "province": province,
                    "language": language,
                    "portal_pass": hash_password(password) if password else password
                }).eq("id", client_id).execute()
                st.success("Profile updated.")

    client_session_key = f"last_prompt_{client_id}"
    client_prefill = st.session_state.get(client_session_key, client.get("last_prompt") or DEFAULT_POLICY_PROMPT)
    client_adv_prompt = st.text_area("Advanced prompt (optional). Use {client} and {regs} to inject context.", value=client_prefill, key="client_adv_prompt", height=160)
    clicked = st.button(f"Generate policy", key="gen_client_policy")
    if clicked:
        if HAVE_GENERATOR:
            with st.spinner("Generating…"):
                md = generate_policy_for_client(client["company_name"], preferred_language=client.get("language"), custom_prompt=(client_adv_prompt or None))
            st.session_state[f"client_last_md"] = md
            st.session_state[f"client_last_fn"] = f"{client['company_name']}_AML_Policy.md"
            st.success("Policy generated. Scroll to download below.")
        else:
            st.warning("Connect policy_generator.py to enable real generation.")

    # Show last result for this client (if any) — fetch once and guard all uses
    client_amd_raw = st.session_state.get("client_last_md")
    if client_amd_raw:
        # normalize all incoming shapes into readable markdown text
        client_amd = normalize_policy_text(client_amd_raw)
        with st.expander("Latest generated policy"):
            st.markdown(client_amd)
        fname_base = st.session_state.get("last_policy_filename", "AML_Policy")
        st.download_button("⬇️ Download latest (Markdown)", client_amd or "", file_name=f"{fname_base}.md", mime="text/markdown")
        try:
            docx_bytes = md_to_docx_bytes(client_amd or "")
            st.download_button("⬇️ Download latest (Word .docx)", docx_bytes, file_name=f"{fname_base}.docx", mime=DOCX_MIME)
        except Exception as e:
            st.caption(f"Could not build .docx: {e}")

def normalize_policy_text(raw: any) -> str:
    """
    Normalize stored/generated payloads into readable markdown:
    - If dict/list, convert with _dict_to_markdown()
    - If text contains Python-style "parts: [...]" (single-quoted repr), parse it and extract 'text'
    - Fully unescape sequences (handles double-escaped "\\n" etc.) using unicode_escape iterative decoding
    """
    if raw is None:
        return ""

    if isinstance(raw, (dict, list)):
        try:
            return _dict_to_markdown(raw)
        except Exception:
            try:
                return json.dumps(raw, indent=2, ensure_ascii=False)
            except Exception:
                raw = str(raw)

    text = str(raw)

    # 1) Try to detect Python-style parts: [...] repr and parse it safely
    m = re.search(r"parts\s*[:=]\s*(\[[\s\S]*\])", text, flags=re.IGNORECASE)
    if m:
        list_repr = m.group(1)
        try:
            obj = ast.literal_eval(list_repr)
            if isinstance(obj, list) and obj and isinstance(obj[0], dict):
                t = obj[0].get("text")
                if isinstance(t, str) and t.strip():
                    text = t
        except Exception:
            # best-effort fallback: extract first quoted block inside the parts repr
            m2 = re.search(r"""['"](.+?)['"]""", list_repr, flags=re.DOTALL)
            if m2:
                text = m2.group(1)

    # 2) If still looks like JSON string with "contents"/"parts", try to extract inner text
    if ("\"parts\"" in text or "'parts'" in text) and ("\"text\"" in text or "'text'" in text):
        m3 = re.search(r"""['"]text['"]\s*[:=]\s*["']([\s\S]+?)["']\s*(?:,|\])""", text, flags=re.DOTALL)
        if m3:
            text = m3.group(1)

    # 3) Iteratively unescape (handle double-escaped sequences)
    for _ in range(4):
        prev = text
        # replace common visible escape sequences first
        text = text.replace("\\r\\n", "\r\n").replace("\\n", "\n").replace("\\t", "\t")
        # collapse doubled backslashes into single-escaped sequences
        text = text.replace("\\\\n", "\\n").replace("\\\\r\\\\n", "\\r\\n")
        # attempt unicode escape decoding (handles \uXXXX escapes and some remaining backslash escapes)
        try:
            decoded = codecs.decode(text, "unicode_escape")
            if decoded != text:
                text = decoded
        except Exception:
            pass
        if text == prev:
            break

    # strip surrounding quotes if present
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1]

    # attempt to fix mojibake before returning
    try:
        text = _fix_mojibake(text)
    except Exception:
        pass

    return text.strip()

# ------------------ ROUTING ------------------
def route():
    ensure_auth()
    role = st.session_state["role"]
    if role == "admin":
        admin_ui()
    elif role == "client":
        client_portal_ui()
    else:
        st.error("Unauthorized access")

route()

