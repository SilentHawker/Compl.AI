import os, time, hashlib
from datetime import datetime, timezone
import streamlit as st
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()
sb: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
PORTAL_BASE = os.getenv("CLIENT_PORTAL_BASE_URL", "http://localhost:8502")

# Try to import your real generator; if missing, show a hint
try:
    from policy_generator import generate_policy_for_client
    HAVE_GENERATOR = True
except Exception:
    HAVE_GENERATOR = False
    def generate_policy_for_client(company_name: str, preferred_language: str | None = None) -> str:
        return f"# AML Policy for {company_name}\n\n(Connect your policy_generator.py to enable real generation.)\n"

st.set_page_config(page_title="ComplianceAI Admin", layout="wide")
st.title("ComplianceAI — Admin Dashboard")

# Helpers
def ts(s): 
    if not s: return "—"
    try: return datetime.fromisoformat(s.replace("Z","")).strftime("%Y-%m-%d %H:%M")
    except: return s

def list_clients():
    return (sb.table("clients")
            .select("id,company_name,province,language,created_at,portal_token,portal_enabled")
            .order("company_name").execute().data or [])

def list_sources():
    return (sb.table("regulations")
            .select("title,source,category,url,last_fetched,last_updated,content_hash")
            .order("title").execute().data or [])

def list_policies(client_id):
    return (sb.table("policies")
            .select("id,regulation_title,regulation_hash,generated_at,language,ai_model")
            .eq("client_id", client_id).order("generated_at", desc=True).execute().data or [])

def add_client(name, prov, lang):
    sb.table("clients").insert({"company_name": name, "province": prov, "language": lang}).execute()

def rotate_token(client_id):
    # set a new portal_token — simplest way is to update from DB using gen_random_uuid()
    sb.rpc("sql", {"q": f"update clients set portal_token = gen_random_uuid() where id = '{client_id}'"}).execute()

# Layout
tab1, tab2, tab3 = st.tabs(["👥 Customers", "🧾 Policies", "📚 Sources"])

with tab1:
    st.subheader("Add a customer")
    with st.form("add_client"):
        name = st.text_input("Company name", "")
        prov = st.selectbox("Province", ["QC","ON","BC","AB","MB","SK","NB","NS","NL","PE","YT","NT","NU"], index=1)
        lang = st.selectbox("Language", ["en","fr"], index=0)
        submitted = st.form_submit_button("Save")
        if submitted:
            if not name.strip():
                st.error("Company name required.")
            else:
                add_client(name.strip(), prov, lang)
                st.success(f"Saved: {name}")
                time.sleep(0.5)
                st.experimental_rerun()

    st.markdown("### All customers")
    clients = list_clients()
    if not clients:
        st.info("No customers yet.")
    else:
        for c in clients:
            with st.container(border=True):
                st.subheader(c["company_name"])
                st.caption(f"{c['province']} • {c['language']} • Added {ts(c['created_at'])}")
                col1, col2, col3 = st.columns([2,2,2])

                with col1:
                    portal_enabled = "✅ enabled" if c.get("portal_enabled") else "❌ disabled"
                    st.text(f"Client portal: {portal_enabled}")
                    link = f"{PORTAL_BASE}/?token={c['portal_token']}" if c.get("portal_token") else "—"
                    st.code(link, language="text")
                    if st.button(f"Rotate link for {c['company_name']}", key="rot_"+c["id"]):
                        rotate_token(c["id"])
                        st.success("Link rotated.")
                        time.sleep(0.5)
                        st.experimental_rerun()

                with col2:
                    lang_sel = st.selectbox(f"Language for {c['company_name']}", ["en","fr"], index=0, key="lang_"+c["id"])
                    if st.button(f"Generate policy", key="gen_"+c["id"]):
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
                    pol = sb.table("policies").select("policy_markdown").eq("id", p["id"]).limit(1).execute().data[0]
                    md = pol["policy_markdown"]
                    st.download_button("Download Markdown", md, file_name=f"{cname}_AML_Policy_{p['id']}.md", mime="text/markdown")
                    with st.expander("Preview"):
                        st.markdown(md)

with tab3:
    st.subheader("Tracked sources")
    srcs = list_sources()
    if not srcs:
        st.info("No sources yet (run your scraper).")
    else:
        for s in srcs:
            with st.container(border=True):
                st.write(f"**{s['title']}** · {s['source']} · *{s.get('category','') or '—'}*")
                st.caption(f"Fetched: {ts(s.get('last_fetched'))} | Updated: {ts(s.get('last_updated'))}")
                st.code(s.get("url") or "—", language="text")
                st.text(f"hash: {s.get('content_hash') or '—'}")
