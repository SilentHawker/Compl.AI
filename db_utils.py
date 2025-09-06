from supabase import create_client, Client
from dotenv import load_dotenv
import os
from typing import Optional

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_KEY environment variables.")

sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def get_client_by_id(client_id: str) -> Optional[dict]:
    res = sb.table("clients").select("*").eq("id", client_id).limit(1).execute()
    return res.data[0] if res.data else None

def get_client_by_name(company_name: str) -> Optional[dict]:
    if not company_name: return None
    res = sb.table("clients").select("*").eq("company_name", company_name).limit(1).execute()
    return res.data[0] if res.data else None

def get_client_by_username(username: str) -> Optional[dict]:
    if not username: return None
    res = sb.table("clients").select("*").eq("portal_user", username).limit(1).execute()
    return res.data[0] if res.data else None

def get_client_by_token(tok: str) -> Optional[dict]:
    if not tok: return None
    res = sb.table("clients").select("*").eq("portal_token", tok).limit(1).execute()
    return res.data[0] if res.data else None

def list_clients() -> list:
    return sb.table("clients").select("id,company_name,province,language,created_at,portal_token,portal_enabled").order("company_name").execute().data or []

def list_policies(client_id: str) -> list:
    return sb.table("policies").select("id,regulation_title,generated_at,language,ai_model").eq("client_id", client_id).order("generated_at", desc=True).execute().data or []