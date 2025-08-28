import os, streamlit as st
from supabase import create_client, Client
from dotenv import load_dotenv
from policy_gen import generate_policy_for_client

load_dotenv()
sb: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

st.set_page_config(page_title="Compliance Policy MVP", layout="wide")
st.title("Compliance Policy MVP (Local)")

tabs = st.tabs(["➕ Add Customer", "🧾 Generate Policy"])

with tabs[0]:
    st.subheader("Add a Canadian MSB customer")
    with st.form("add_client"):
        name = st.text_input("Company name", "")
        province = st.selectbox("Province", ["QC","ON","BC","AB","MB","SK","NB","NS","NL","PE","YT","NT","NU"])
        language = st.selectbox("Language", ["en","fr"])
        submitted = st.form_submit_button("Save Customer")
        if submitted:
            if not name.strip():
                st.error("Company name required.")
            else:
                sb.table("clients").insert({
                    "company_name": name.strip(),
                    "province": province,
                    "language": language
                }).execute()
                st.success(f"Saved client: {name}")

with tabs[1]:
    st.subheader("Generate AML Policy (from all relevant FINTRAC sources for MSBs)")

    # Load clients
    clients = sb.table("clients").select("id,company_name").order("company_name").execute().data or []
    names = [c["company_name"] for c in clients]
    sel = st.selectbox("Select client", names)

    colA, colB = st.columns([1,1])
    with colA:
        language = st.selectbox("Language", ["en","fr"], index=0)

    run = st.button("Generate Policy Now")
    if run and sel:
        with st.spinner("Generating..."):
            doc = generate_policy_for_client(sel, preferred_language=language)
        st.success("Done.")
        st.download_button("Download Markdown", doc, file_name=f"{sel}_AML_Policy.md", mime="text/markdown")
        st.markdown("---")
        st.markdown(doc)
