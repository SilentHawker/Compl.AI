# app.py
import os, time, uuid
from datetime import datetime
import streamlit as st
from supabase import create_client, Client
from dotenv import load_dotenv
import bcrypt

# ---- Streamlit compatibility shims (new & old APIs) ----
import streamlit as st

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

sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

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

def list_clients():
    return (sb.table("clients")
             .select("id,company_name,province,language,created_at,portal_token,portal_enabled")
             .order("company_name").execute().data or [])

def list_sources():
    return (sb.table("regulations")
             .select("id,title,source,category,url,last_fetched,last_updated,content_hash,current_version_no")
             .order("title").execute().data or [])

def list_policies(client_id):
    return (sb.table("policies")
             .select("id,regulation_title,regulation_hash,generated_at,language,ai_model")
             .eq("client_id", client_id)
             .order("generated_at", desc=True).execute().data or [])

def add_client(name, prov, lang):
    sb.table("clients").insert({"company_name": name, "province": prov, "language": lang}).execute()

def rotate_token_python(client_id):
    new_tok = str(uuid.uuid4())
    sb.table("clients").update({"portal_token": new_tok}).eq("id", client_id).execute()
    return new_tok

def get_client_by_token(tok):
    if not tok: return None
    res = (sb.table("clients")
           .select("id,company_name,province,language,portal_enabled")
           .eq("portal_token", tok).limit(1).execute())
    return res.data[0] if res.data else None

def get_client_by_username(username):
    if not username: return None
    res = (sb.table("clients")
           .select("id,company_name,province,language,portal_enabled,portal_pass")
           .eq("portal_user", username).limit(1).execute())
    st.write(f"Debug: Query result for username '{username}': {res.data}")
    return res.data[0] if res.data else None

def get_client_by_id(client_id: str):
    res = (sb.table("clients")
           .select("id,company_name,province,language,portal_enabled")
           .eq("id", client_id)
           .limit(1)
           .execute())
    return res.data[0] if res.data else None

def get_policies_by_client(client_id):
    return (sb.table("policies")
            .select("id,regulation_title,generated_at,language,ai_model")
            .eq("client_id", client_id)
            .order("generated_at", desc=True).execute().data or [])

# -------- Versioning helpers --------
def list_registrations_for_versions():
    return (sb.table("regulations")
             .select("id,title,source,category,url,current_version_no,last_updated,last_fetched")
             .order("source", desc=False)
             .order("title", desc=False)
             .execute().data or [])

def list_versions(regulation_id: str):
    return (sb.table("regulation_versions")
             .select("id,version_no,content_hash,scraped_at,change_summary")
             .eq("regulation_id", regulation_id)
             .order("version_no", desc=True)
             .execute().data or [])

def get_version_content_by_no(regulation_id: str, version_no: int):
    rows = (sb.table("regulation_versions")
              .select("id,content,content_hash,scraped_at,change_summary")
              .eq("regulation_id", regulation_id)
              .eq("version_no", version_no)
              .limit(1)
              .execute().data or [])
    return rows[0] if rows else None

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
                if client and client.get("portal_enabled", True) and p.lower() == client.get("portal_pass", "").lower():
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
                        lang_sel = st.selectbox(f"Language for {c['company_name']}", ["en", "fr"], index=0, key="lang_" + c["id"])
                        if st.button(f"Generate policy", key="gen_" + c["id"]):
                            if HAVE_GENERATOR:
                                with st.spinner("Generating…"):
                                    md = generate_policy_for_client(c["company_name"], preferred_language=lang_sel)
                                st.success("Policy generated.")
                                st.download_button("Download Markdown", md, file_name=f"{c['company_name']}_AML_Policy.md", mime="text/markdown")
                            else:
                                st.warning("Connect policy_generator.py to enable real generation.")

                    with col3:
                        pols = list_policies(c["id"])
                        if pols:
                            st.write("Latest policies:")
                            for p in pols[:3]:
                                st.caption(f"• {p['regulation_title']} [{p['language']}] via {p['ai_model']} · {ts(p['generated_at'])}")
                        else:
                            st.caption("No policies yet.")

                    # Add portal access management
                    with st.expander("Portal access"):
                        cur_user = c.get("portal_user") or ""
                        cur_enabled = bool(c.get("portal_enabled"))
                        u = st.text_input(f"Username for {c['company_name']}", value=cur_user, key=f"pu_{c['id']}")
                        p = st.text_input("Password", value="", type="password", key=f"pp_{c['id']}")
                        en = st.checkbox("Enabled", value=cur_enabled, key=f"pe_{c['id']}")
                        if st.button("Save portal creds", key=f"savep_{c['id']}"):
                            if not u.strip():
                                st.error("Username required.")
                            elif not p and not cur_user:
                                st.error("Password required for first-time setup.")
                            else:
                                set_client_portal_creds(c["id"], u.strip(), p or (c.get("portal_pass") or ""), en)
                                st.success("Portal credentials saved.")
                                _rerun()

    # ---------- Policies ----------
    with tab2:
        st.subheader("Browse policies by client")
        clients = list_clients()
        if not clients:
            st.info("No customers yet.")
        else:
            name_to_id = {c["company_name"]: c["id"] for c in clients}
            cname = st.selectbox("Customer", list(name_to_id.keys()))
            cid = name_to_id[cname]
            rows = list_policies(cid)
            if not rows:
                st.info("No policies yet for this customer.")
            else:
                for p in rows:
                    with st.container(border=True):
                        st.subheader(p["regulation_title"])
                        st.caption(f"{p['language']} · {p['ai_model']} · {ts(p['generated_at'])}")
                        pol = (sb.table("policies").select("policy_markdown")
                               .eq("id", p["id"]).limit(1).execute().data[0])
                        md = pol["policy_markdown"]
                        st.download_button("Download Markdown", md,
                                           file_name=f"{cname}_AML_Policy_{p['id']}.md", mime="text/markdown")
                        with st.expander("Preview"):
                            st.markdown(md)

    # ---------- Sources ----------
    with tab3:
        st.subheader("Tracked sources")
        srcs = list_sources()
        if not srcs:
            st.info("No sources yet (run your scraper).")
        else:
            for s in srcs:
                with st.container(border=True):
                    st.write(f"**{s['title']}** · {s['source']} · *{s.get('category','') or '—'}*")
                    st.caption(f"Fetched: {ts(s.get('last_fetched'))} | Updated: {ts(s.get('last_updated'))} | Head v{(s.get('current_version_no') or 0)}")
                    st.code(s.get("url") or "—", language="text")
                    st.text(f"hash: {s.get('content_hash') or '—'}")

    # ---------- Versions ----------
    with tab4:
        st.subheader("Regulation versions (append-only audit)")

        regs = list_registrations_for_versions()
        if not regs:
            st.info("No regulations found. Seed by running your scraper.")
        else:
            options = [f"{r['source']} • {r['title']}  —  {r['url']}" for r in regs]
            sel = st.selectbox("Select a regulation", options, index=0)
            sel_row = regs[options.index(sel)]
            reg_id = sel_row["id"]

            st.caption(f"Head version: v{sel_row.get('current_version_no') or 0}  •  Updated: {ts(sel_row.get('last_updated'))}  •  Fetched: {ts(sel_row.get('last_fetched'))}")

            vers = list_versions(reg_id)
            if not vers:
                st.info("No versions recorded yet.")
            else:
                st.markdown("### All versions")
                for v in vers:
                    with st.container(border=True):
                        st.write(f"**v{v['version_no']}**  •  hash: `{v['content_hash']}`  •  scraped: {ts(v['scraped_at'])}")
                        if v.get("change_summary"):
                            with st.expander("AI change summary"):
                                st.json(v["change_summary"])

                        with st.expander("Preview this version"):
                            vcontent = get_version_content_by_no(reg_id, v["version_no"])
                            if vcontent:
                                st.text_area("Content (read-only)", value=vcontent["content"], height=220)
                                st.download_button(
                                    "Download version as .txt",
                                    vcontent["content"],
                                    file_name=f"reg_{reg_id}_v{v['version_no']}.txt",
                                    mime="text/plain"
                                )
                            else:
                                st.warning("Content not found for this version.")

                st.markdown("---")
                st.markdown("### Compare two versions")
                vnos = sorted([v["version_no"] for v in vers], reverse=True)
                colA, colB = st.columns(2)
                with colA:
                    v_left = st.selectbox("Left version", vnos, index=0, key="vleft")
                with colB:
                    v_right = st.selectbox("Right version", vnos, index=min(1, len(vnos)-1), key="vright")

                if st.button("Show diff"):
                    left = get_version_content_by_no(reg_id, v_left)
                    right = get_version_content_by_no(reg_id, v_right)
                    if not left or not right:
                        st.error("Could not load one of the versions.")
                    else:
                        import difflib
                        diff = difflib.unified_diff(
                            left["content"].splitlines(),
                            right["content"].splitlines(),
                            fromfile=f"v{v_left}",
                            tofile=f"v{v_right}",
                            lineterm=""
                        )
                        diff_text = "\n".join(diff) or "(No differences)"
                        st.code(diff_text, language="diff")

    # Add a section to set portal credentials
    st.subheader("Set Client Portal Credentials")
    clients = list_clients()
    client_names = {c["company_name"]: c["id"] for c in clients}
    selected_client = st.selectbox("Select Client", list(client_names.keys()))
    client_id = client_names[selected_client]

    user = st.text_input("Portal Username", "")
    pwd = st.text_input("Portal Password", "", type="password")
    enabled = st.checkbox("Enable Portal", value=True)

    if st.button("Set Credentials"):
        if user.strip() and pwd:
            set_client_portal_creds(client_id, user, pwd, enabled)
            st.success(f"Portal credentials updated for {selected_client}")
        else:
            st.error("Username and password are required.")

# ------------------ CLIENT UI ------------------
def client_ui():
    st.title("Client Portal")
    logout_button()

    # Prefer session-bound client (username-bound login)
    bound_id = st.session_state.get("client_id")
    client = None
    if bound_id:
        client = get_client_by_id(bound_id)

    # Fallback to token flow only if not bound by username/password
    if not client:
        st.caption("Private access. Use your portal link.")
        qp = _get_qp()
        token = qp.get("token")
        if isinstance(token, list):
            token = token[0] if token else None

        if not token:
            st.warning("Missing portal token. Paste it below.")
            t_in = st.text_input("Portal token (from admin link)", "")
            if t_in:
                qp["token"] = t_in
                _set_qp(qp)
                _rerun()
            st.stop()

        client = get_client_by_token(token)

    if not client:
        st.error("Invalid or expired portal link.")
        st.stop()
    if not client.get("portal_enabled", True):
        st.error("This portal is disabled. Contact your admin.")
        st.stop()

    # Display client details
    st.subheader(client["company_name"])
    st.caption(f"Province: {client['province']} • Language: {client['language']}")

    # Show policies
    rows = get_policies_by_client(client["id"])
    if rows:
        st.markdown("### Your Policies")
        for p in rows:
            with st.container(border=True):
                st.subheader(p["regulation_title"])
                st.caption(f"{p['language']} • {p['ai_model']} • {ts(p['generated_at'])}")
                pol = (sb.table("policies").select("policy_markdown")
                       .eq("id", p["id"]).limit(1).execute().data[0])
                md = pol["policy_markdown"]
                st.download_button("Download Markdown", md,
                                   file_name=f"{client['company_name']}_AML_Policy_{p['id']}.md",
                                   mime="text/markdown")
                with st.expander("Preview"):
                    st.markdown(md)
    else:
        st.info("No policies yet for your organization.")

    # Generate a new policy
    st.markdown("---")
    st.subheader("Generate a new policy")
    lang_sel = st.selectbox("Language", ["en", "fr"], index=0)
    if st.button("Generate now"):
        if HAVE_GENERATOR:
            with st.spinner("Generating…"):
                md = generate_policy_for_client(client["company_name"], preferred_language=lang_sel)
            st.success("Policy generated.")
            st.download_button("Download Markdown", md,
                               file_name=f"{client['company_name']}_AML_Policy.md",
                               mime="text/markdown")
            time.sleep(0.4)
            _rerun()
        else:
            st.warning("The generator is not connected. Ask your admin to enable it.")

# ------------------ ROUTER ------------------
def show_login_and_route():
    # initial state
    if "authed" not in st.session_state:
        st.session_state["authed"] = False
        st.session_state["role"] = None
    # not authed => show login
    if not st.session_state["authed"]:
        show_login()
        return
    # route by role
    role = st.session_state.get("role")
    if role == "admin":
        admin_ui()
    elif role == "client":
        client_ui()
    else:
        st.error("Unknown role. Please log in again.")
        st.session_state.clear()

if __name__ == "__main__":
    show_login_and_route()

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def check_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
