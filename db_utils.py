from supabase import create_client, Client
from dotenv import load_dotenv
import os
from typing import Optional, List, Dict, Any

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_KEY environment variables.")

sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---------- Clients ----------
def get_client_by_id(client_id: str) -> Optional[dict]:
    res = sb.table("clients").select("*").eq("id", client_id).limit(1).execute()
    return res.data[0] if res.data else None

def get_client_by_name(company_name: str) -> Optional[dict]:
    if not company_name: 
        return None
    res = sb.table("clients").select("*").eq("company_name", company_name).limit(1).execute()
    return res.data[0] if res.data else None

def get_client_by_username(username: str) -> Optional[dict]:
    if not username: 
        return None
    res = sb.table("clients").select("*").eq("portal_user", username).limit(1).execute()
    return res.data[0] if res.data else None

def get_client_by_token(tok: str) -> Optional[dict]:
    if not tok: 
        return None
    res = sb.table("clients").select("*").eq("portal_token", tok).limit(1).execute()
    return res.data[0] if res.data else None

def list_clients() -> List[Dict[str, Any]]:
    return (sb.table("clients")
              .select("id,company_name,province,language,created_at,portal_token,portal_enabled,portal_user")
              .order("company_name")
              .execute().data or [])

# ---------- Policies ----------
def list_policies(client_id: str) -> List[Dict[str, Any]]:
    return (sb.table("policies")
              .select("id,regulation_title,generated_at,language,ai_model")
              .eq("client_id", client_id)
              .order("generated_at", desc=True)
              .execute().data or [])

# alias used by client portal code
def get_policies_by_client(client_id: str) -> List[Dict[str, Any]]:
    return list_policies(client_id)

# ---------- Regulations (Sources) ----------
def list_sources() -> List[Dict[str, Any]]:
    return (sb.table("regulations")
              .select("id,title,source,category,url,last_fetched,last_updated,content_hash,current_version_no")
              .order("title")
              .execute().data or [])

# ---------- Versioning ----------
def list_registrations_for_versions() -> List[Dict[str, Any]]:
    return (sb.table("regulations")
              .select("id,title,source,category,url,current_version_no,last_updated,last_fetched")
              .order("source")
              .order("title")
              .execute().data or [])

def list_versions(regulation_id: str) -> List[Dict[str, Any]]:
    return (sb.table("regulation_versions")
              .select("id,version_no,content_hash,scraped_at,change_summary")
              .eq("regulation_id", regulation_id)
              .order("version_no", desc=True)
              .execute().data or [])

def get_version_content_by_no(regulation_id: str, version_no: int) -> Optional[Dict[str, Any]]:
    rows = (sb.table("regulation_versions")
              .select("id,content,content_hash,scraped_at,change_summary")
              .eq("regulation_id", regulation_id)
              .eq("version_no", version_no)
              .limit(1)
              .execute().data or [])
    return rows[0] if rows else None
