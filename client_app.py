import os, time
import streamlit as st
from supabase import create_client, Client
from dotenv import load_dotenv

# Try to import your real generator; optional
try:
    from policy_generator import generate_policy_for_client
    HAVE_GENERATOR = True
except Exception:
    HAVE_GENERATOR = False
    def generate_policy_for_client(company_name: str, preferred_language: str | None = None) -> str:
        return f"# AML Policy for {company_name}\n\n(Connect policy_generator.py to enable real generation.)\n"

load_dotenv()
sb: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

st.set_page_config(page_title="Client Portal", layout="wide")
st.title("Client Portal")

# Read token
qp = st.experimental_get_query_params()
token = (qp.get("token") or [None])[0]

st.caption("Private access link — if this link is shared, rotate it from the admin dashboard.")

def get_client_by_token(tok: str):
    if not tok: return None
    res = sb.table("clients").select("id,company_name,province,language,portal_enabled").eq("portal_token", tok).limit(1).execute()
    return res.data[0] if res.data else None

if not token:
    st.warning("Missing token. Ask your admin for your portal link.")
    token = st.text_input("Paste your portal token", "")
    if token:
        st.experimental_set_query_params(token=token)
        st.experimental_rerun()
    st.stop()

client = get_client_by_token(token)
if not client:
    st.error("Invalid or expired portal link.")
    st.stop()
if not client.get("portal_enabled", True):
    st.error("This portal is disabled. Contact your admin.")
    st.stop()

st.subheader(client["company_name"])
st.caption(f"Province: {client['province']} • Language: {client['language']}")

# Show latest policies
rows = (sb.table("policies")
        .select("id,regulation_title,generated_at,language,ai_model")
        .eq("client_id", client["id"]).order("generated_at", desc=True).execute().data or [])

if rows:
    st.markdown("### Your Policies")
    for p in rows:
        with st.container(border=True):
            st.subheader(p["regulation_title"])
            st.caption(f"{p['language']} · {p['ai_model']} · {p['generated_at']}")
            pol = sb.table("policies").select("policy_markdown").eq("id", p["id"]).limit(1).execute().data[0]
            md = pol["policy_markdown"]
            st.download_button("Download Markdown", md, file_name=f"{client['company_name']}_AML_Policy_{p['id']}.md", mime="text/markdown")
            with st.expander("Preview"):
                st.markdown(md)
else:
    st.info("No policies yet for your organization.")

# Allow on-demand generation (optional)
st.markdown("---")
st.subheader("Generate a new policy")
lang_sel = st.selectbox("Language", ["en","fr"], index=0)
if st.button("Generate now"):
    if HAVE_GENERATOR:
        with st.spinner("Generating…"):
            md = generate_policy_for_client(client["company_name"], preferred_language=lang_sel)
        st.success("Policy generated.")
        st.download_button("Download Markdown", md, file_name=f"{client['company_name']}_AML_Policy.md", mime="text/markdown")
        time.sleep(0.5)
        st.experimental_rerun()
    else:
        st.warning("The generator is not connected. Ask your admin to enable it.")
