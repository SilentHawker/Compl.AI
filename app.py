# app.py
import os
import json
from datetime import datetime
from io import BytesIO
from typing import Optional
import re
import ast
import codecs
import bcrypt
from dotenv import load_dotenv

# GCP Secret Manager
from google.cloud import secretmanager
from google.api_core.exceptions import GoogleAPIError

# FastAPI backend
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio

load_dotenv()

# try to import generator
try:
    from policy_gen import generate_policy_for_client
    HAVE_GENERATOR = True
except Exception:
    HAVE_GENERATOR = False
    def generate_policy_for_client(company_name: str, preferred_language: Optional[str] = None, custom_prompt: Optional[str] = None) -> str:
        return f"# AML Policy for {company_name}\n\n(Generator not available. Connect policy_gen.py to enable real generation.)\n"

# DB helpers (imported from db_utils)
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

# lightweight wrappers to keep previous function names
def list_clients(): return db_list_clients()
def list_policies(client_id: Optional[str] = None): return db_list_policies(client_id)
def get_client_by_token(tok): return db_get_client_by_token(tok)
def get_client_by_username(username): return db_get_client_by_username(username)
def get_client_by_id(client_id): return db_get_client_by_id(client_id)
def get_client_by_name(company_name): return db_get_client_by_name(company_name)
def list_sources(): return db_list_sources()
def list_registrations_for_versions(): return db_list_regs_for_versions()
def list_versions(regulation_id: Optional[str] = None): return db_list_versions(regulation_id)
def get_version_content_by_no(regulation_id, version_no): return db_get_version_content_by_no(regulation_id, version_no)
def get_policies_by_client(client_id): return db_get_policies_by_client(client_id)

# ENV
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")
API_KEY = os.getenv("API_KEY", "dev-key")

# FastAPI app
app = FastAPI(title="Compl.AI Backend")

# Configure CORS (adjust origins in production)
origins = os.getenv("CORS_ORIGINS", "*").split(",") if os.getenv("CORS_ORIGINS") else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------- helpers ----------
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False
    if stored.startswith("$2a$") or stored.startswith("$2b$") or stored.startswith("$2y$"):
        try:
            return bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8"))
        except Exception:
            return False
    return password == stored

def _fix_mojibake(text: str) -> str:
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

def normalize_policy_text(raw: any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, (dict, list)):
        try:
            return json.dumps(raw, indent=2, ensure_ascii=False)
        except Exception:
            raw = str(raw)
    text = str(raw)
    # extract parts[...] patterns
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
            m2 = re.search(r"""['"](.+?)['"]""", list_repr, flags=re.DOTALL)
            if m2:
                text = m2.group(1)
    # unescape visible sequences
    for _ in range(4):
        prev = text
        text = text.replace("\\r\\n", "\r\n").replace("\\n", "\n").replace("\\t", "\t")
        text = text.replace("\\\\n", "\\n").replace("\\\\r\\\\n", "\\r\\n")
        try:
            decoded = codecs.decode(text, "unicode_escape")
            if decoded != text:
                text = decoded
        except Exception:
            pass
        if text == prev:
            break
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1]
    try:
        text = _fix_mojibake(text)
    except Exception:
        pass
    return text.strip()

def _fetch_secret_from_gcp(secret_id: str, project_id: Optional[str] = None) -> Optional[str]:
    """
    Access Secret Manager 'projects/{project}/secrets/{secret_id}/versions/latest'
    Returns the secret string or None on failure.
    """
    try:
        project = project_id or os.getenv("GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
        if not project:
            print("GCP project not set in GCP_PROJECT / GOOGLE_CLOUD_PROJECT env var; skipping secret fetch")
            return None
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project}/secrets/{secret_id}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        payload = response.payload.data.decode("UTF-8")
        return payload
    except GoogleAPIError as e:
        print(f"GCP Secret Manager API error fetching {secret_id}: {e}")
    except Exception as e:
        print(f"Error fetching secret {secret_id}: {e}")
    return None

@app.on_event("startup")
async def _load_secrets_on_startup():
    """
    Attempt to load required secrets from GCP Secret Manager into process environment.
    Fallback: keep any existing environment variables (e.g. from .env or system).
    """
    # list of secret names you store in GCP Secret Manager
    secret_names = {
        "API_KEY": os.getenv("SECRET_API_KEY_NAME", "COMPLAI_API_KEY"),
        "SUPABASE_URL": os.getenv("SECRET_SUPABASE_URL_NAME", "SUPABASE_URL"),
        "SUPABASE_KEY": os.getenv("SECRET_SUPABASE_KEY_NAME", "SUPABASE_KEY"),
        "LLM_KEY": os.getenv("SECRET_LLM_KEY_NAME", "LLM_API_KEY"),
        "ADMIN_USER": os.getenv("SECRET_ADMIN_USER_NAME", "ADMIN_USER"),
        "ADMIN_PASS": os.getenv("SECRET_ADMIN_PASS_NAME", "ADMIN_PASS")
    }
    project = os.getenv("GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project:
        print("No GCP project env var found; skipping secret manager fetch (set GCP_PROJECT for automatic fetch).")
        return

    for env_name, secret_id in secret_names.items():
        # Do not overwrite if env var already present locally (local dev precedence)
        if os.getenv(env_name):
            continue
        try:
            val = _fetch_secret_from_gcp(secret_id, project_id=project)
            if val is not None:
                os.environ[env_name] = val
                print(f"Loaded secret into env: {env_name}")
        except Exception as e:
            print(f"Failed to load secret {secret_id}: {e}")

# -------- request/response models ----------
class GenerateRequest(BaseModel):
    company_name: str
    custom_prompt: Optional[str] = None
    language: Optional[str] = None

class GenerateResponse(BaseModel):
    markdown: str

# -------- endpoints ----------
@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/clients", dependencies=[Depends(require_api_key)])
async def api_list_clients():
    return list_clients()

@app.get("/clients/{client_id}", dependencies=[Depends(require_api_key)])
async def api_get_client(client_id: str):
    c = get_client_by_id(client_id)
    if not c:
        raise HTTPException(status_code=404, detail="client not found")
    return c

@app.post("/clients", dependencies=[Depends(require_api_key)])
async def api_add_client(payload: dict):
    name = payload.get("company_name")
    prov = payload.get("province", "N/A")
    lang = payload.get("language", "en")
    if not name:
        raise HTTPException(status_code=400, detail="company_name required")
    add_res = sb.table("clients").insert({"company_name": name, "province": prov, "language": lang}).execute()
    return {"ok": True, "result": add_res.data if hasattr(add_res, "data") else None}

# Update require_api_key to read from env at call time
def require_api_key(x_api_key: str = Header(...)):
    expected = os.getenv("API_KEY")
    if not expected:
        # if no API_KEY in env, be explicit and reject
        raise HTTPException(status_code=500, detail="Server API key not configured")
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key

@app.post("/auth/login")
async def api_login(username: str, password: str, role: str = "client"):
    if role == "admin":
        admin_user = os.getenv("ADMIN_USER", "admin")
        admin_pass = os.getenv("ADMIN_PASS", "admin123")
        if username == admin_user and password == admin_pass:
            return {"token": os.getenv("API_KEY"), "role": "admin"}
        raise HTTPException(status_code=401, detail="invalid admin credentials")
    # client auth
    client = get_client_by_username(username)
    if not client:
        raise HTTPException(status_code=404, detail="client not found")
    if not client.get("portal_enabled", True):
        raise HTTPException(status_code=403, detail="portal disabled")
    if verify_password(password, client.get("portal_pass", "") or ""):
        return {"token": API_KEY, "role": "client", "client_id": client["id"]}
    raise HTTPException(status_code=401, detail="invalid credentials")

@app.post("/generate", response_model=GenerateResponse, dependencies=[Depends(require_api_key)])
async def api_generate(req: GenerateRequest):
    client = get_client_by_name(req.company_name)
    if not client:
        raise HTTPException(status_code=404, detail="client not found")
    loop = asyncio.get_running_loop()
    try:
        md = await loop.run_in_executor(None, generate_policy_for_client, req.company_name, req.language, req.custom_prompt)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    md = normalize_policy_text(md)
    return {"markdown": md}

@app.get("/policies", dependencies=[Depends(require_api_key)])
async def api_list_policies(client_id: Optional[str] = None):
    return list_policies(client_id)

@app.get("/policies/{policy_id}", dependencies=[Depends(require_api_key)])
async def api_get_policy(policy_id: str):
    res = sb.table("policies").select("*").eq("id", policy_id).limit(1).execute()
    if not res or not getattr(res, "data", None):
        raise HTTPException(status_code=404, detail="policy not found")
    p = res.data[0]
    p["policy_markdown"] = normalize_policy_text(p.get("policy_markdown") or p.get("policy_md"))
    return p

@app.get("/policies/client/{client_id}", dependencies=[Depends(require_api_key)])
async def api_get_policies_by_client(client_id: str):
    return get_policies_by_client(client_id)

@app.get("/sources", dependencies=[Depends(require_api_key)])
async def api_list_sources():
    return list_sources()

@app.get("/versions", dependencies=[Depends(require_api_key)])
async def api_list_versions(regulation_id: Optional[str] = None):
    try:
        return list_versions(regulation_id)
    except TypeError:
        return list_versions(None)

@app.get("/versions/{reg_id}/{version_no}", dependencies=[Depends(require_api_key)])
async def api_get_version_content(reg_id: str, version_no: int):
    content = get_version_content_by_no(reg_id, version_no)
    return {"content": normalize_policy_text(content)}

# utility endpoint to download a policy as .md (returns raw text)
@app.get("/policies/{policy_id}/download", dependencies=[Depends(require_api_key)])
async def api_download_policy(policy_id: str):
    res = sb.table("policies").select("*").eq("id", policy_id).limit(1).execute()
    if not res or not getattr(res, "data", None):
        raise HTTPException(status_code=404, detail="policy not found")
    p = res.data[0]
    md = normalize_policy_text(p.get("policy_markdown") or p.get("policy_md"))
    return {"filename": f"Policy_{policy_id}.md", "content": md}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8502, reload=True)

