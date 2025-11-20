from supabase import create_client, Client
from dotenv import load_dotenv
import os
from typing import Optional, List, Dict, Any

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_KEY in environment")

# create supabase client once
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
def list_policies(client_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Return policies for a given client_id. If client_id is None, return all policies.
    Attempt to order by created_at if available; fall back to an unordered query if the column does not exist.
    """
    try:
        if client_id:
            res = sb.table("policies").select("*").eq("client_id", client_id).order("created_at", desc=True).execute()
        else:
            res = sb.table("policies").select("*").order("created_at", desc=True).execute()
    except Exception:
        # fallback if created_at column doesn't exist (or other Postgrest errors)
        if client_id:
            res = sb.table("policies").select("*").eq("client_id", client_id).execute()
        else:
            res = sb.table("policies").select("*").execute()
    return res.data or []

# alias used by client portal code
def get_policies_by_client(client_id: str) -> List[Dict[str, Any]]:
    return list_policies(client_id)

# ---------- Regulations (Sources) ----------
def list_sources() -> List[Dict[str, Any]]:
    return (sb.table("regulations")
              .select("id,name,source,category,url,last_fetched,last_updated,content_hash,current_version_no")
              .order("name")
              .execute().data or [])

# ---------- Versioning ----------
def list_registrations_for_versions() -> List[Dict[str, Any]]:
    return (sb.table("regulations")
              .select("id,name,source,category,url,current_version_no,last_updated,last_fetched")
              .order("source")
              .order("name")
              .execute().data or [])

def list_versions(regulation_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Return versions for a given regulation_id. If regulation_id is None, return all versions.
    Safe fallback if ordering column does not exist.
    """
    try:
        if regulation_id:
            res = sb.table("regulation_versions").select("*").eq("regulation_id", regulation_id).order("version_no", desc=True).execute()
        else:
            # return all versions ordered by regulation_id then version_no where possible
            res = sb.table("regulation_versions").select("*").order("regulation_id", desc=False).order("version_no", desc=True).execute()
    except Exception:
        # fallback to simpler queries if schema differs
        if regulation_id:
            res = sb.table("regulation_versions").select("*").eq("regulation_id", regulation_id).execute()
        else:
            res = sb.table("regulation_versions").select("*").execute()
    return res.data or []

def get_version_content_by_no(regulation_id: str, version_no: int) -> Optional[Dict[str, Any]]:
    rows = (sb.table("regulation_versions")
              .select("id,content,content_hash,scraped_at,change_summary")
              .eq("regulation_id", regulation_id)
              .eq("version_no", version_no)
              .limit(1)
              .execute().data or [])
    return rows[0] if rows else None

def get_admin_by_email(email: str) -> Optional[dict]:
    if not email:
        return None
    res = sb.table("admin_users").select("*").eq("email", email).limit(1).execute()
    return res.data[0] if res.data else None

def create_admin_user(email: str, password_hash: str, full_name: str, role: str = "admin") -> dict:
    data = {
        "email": email,
        "password_hash": password_hash,
        "full_name": full_name,
        "role": role,
        "is_active": True
    }
    res = sb.table("admin_users").insert(data).execute()
    return res.data[0] if res.data else None

def update_admin_last_login(admin_id: str):
    sb.table("admin_users").update({"last_login": "now()"}).eq("id", admin_id).execute()

# ========== Master Prompts (Admin Only) ==========
def list_master_prompts(is_active_only: bool = True) -> List[Dict[str, Any]]:
    query = sb.table("master_prompts").select("*")
    if is_active_only:
        query = query.eq("is_active", True)
    return query.order("category").order("name").execute().data or []

def get_master_prompt_by_id(prompt_id: str) -> Optional[dict]:
    res = sb.table("master_prompts").select("*").eq("id", prompt_id).limit(1).execute()
    return res.data[0] if res.data else None

def get_master_prompt_by_name(name: str) -> Optional[dict]:
    res = sb.table("master_prompts").select("*").eq("name", name).limit(1).execute()
    return res.data[0] if res.data else None

def create_master_prompt(name: str, prompt_text: str, description: str = None, 
                         category: str = None, created_by: str = None) -> dict:
    data = {
        "name": name,
        "prompt_text": prompt_text,
        "description": description,
        "category": category,
        "created_by": created_by,
        "is_active": True
    }
    res = sb.table("master_prompts").insert(data).execute()
    return res.data[0] if res.data else None

def update_master_prompt(prompt_id: str, **updates) -> dict:
    """Update a master prompt and return the updated record"""
    from datetime import datetime
    updates["updated_at"] = datetime.utcnow().isoformat()
    res = sb.table("master_prompts").update(updates).eq("id", prompt_id).execute()
    if not res.data:
        raise Exception(f"Failed to update master prompt {prompt_id}")
    return res.data[0]

# ========== Policies (Enhanced) ==========
def create_policy(client_id: str, name: str, content: str = None, markdown: str = None,
                  master_prompt_id: str = None, language: str = "en", 
                  status: str = "draft") -> dict:
    data = {
        "client_id": client_id,
        "name": name,
        "content": content,
        "markdown": markdown,
        "master_prompt_id": master_prompt_id,
        "language": language,
        "status": status
    }
    res = sb.table("policies").insert(data).execute()
    return res.data[0] if res.data else None

def update_policy(policy_id: str, **updates) -> dict:
    updates["updated_at"] = "now()"
    res = sb.table("policies").update(updates).eq("id", policy_id).execute()
    return res.data[0] if res.data else None

def get_policy_by_id(policy_id: str) -> Optional[dict]:
    res = sb.table("policies").select("*").eq("id", policy_id).limit(1).execute()
    return res.data[0] if res.data else None

# ========== Client Policies (Many-to-Many) ==========
def assign_policy_to_client(client_id: str, policy_id: str, assigned_by: str = None) -> dict:
    data = {
        "client_id": client_id,
        "policy_id": policy_id,
        "assigned_by": assigned_by
    }
    res = sb.table("client_policies").insert(data).execute()
    return res.data[0] if res.data else None

def get_policies_for_client(client_id: str) -> List[Dict[str, Any]]:
    """Get all policies assigned to a client via client_policies"""
    res = sb.table("client_policies").select(
        "policy_id, policies(*)"
    ).eq("client_id", client_id).execute()
    return [item["policies"] for item in (res.data or []) if item.get("policies")]
