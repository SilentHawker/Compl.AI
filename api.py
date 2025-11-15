import os
from dotenv import load_dotenv

# load .env from the repo root (explicit path so working-dir mismatches don't break it)
_here = os.path.dirname(__file__)
load_dotenv(dotenv_path=os.path.join(_here, ".env"))

from fastapi import FastAPI, HTTPException, Request, Header, Depends, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from postgrest.exceptions import APIError
from pydantic import BaseModel
import os
from dotenv import load_dotenv

# load .env from the repo root (explicit path so working-dir mismatches don't break it)
_here = os.path.dirname(__file__)
load_dotenv(dotenv_path=os.path.join(_here, ".env"))

from fastapi import FastAPI, HTTPException, Request, Header, Depends, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from postgrest.exceptions import APIError
from pydantic import BaseModel
import os
import asyncio
from policy_gen import generate_policy_for_client
from db_utils import get_client_by_name, sb
from typing import Optional
import bcrypt
import jwt
from datetime import datetime, timedelta

# JWT secret (set via env in production)
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "your-secret-key-change-in-prod")

def create_access_token(admin_id: str, role: str) -> str:
    """Create a short-lived JWT for admin sessions."""
    payload = {
        "sub": admin_id,
        "role": role,
        "exp": datetime.utcnow() + timedelta(hours=24)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")

app = FastAPI(title="Compl.AI Backend")

# CORS configuration - must be permissive for Google AI Studio
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins in dev
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["*"],  # Allow all headers including ngrok-skip-browser-warning
    expose_headers=["*"]
)

def require_api_key(x_api_key: str = Header(...)):
    # read and normalize API key at call time
    expected = os.getenv("API_KEY") or ""
    expected = expected.strip()
    if not expected:
        # helpful log for local dev (no secret printed)
        print("ERROR: API_KEY not found in environment (.env not loaded or missing).")
        raise HTTPException(status_code=500, detail="Server API key not configured")
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key

class GenerateRequest(BaseModel):
    company_name: str
    custom_prompt: str | None = None
    language: str | None = None

class GenerateResponse(BaseModel):
    markdown: str

@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/api/v1/generate", response_model=GenerateResponse, dependencies=[Depends(require_api_key)])
async def generate(req: GenerateRequest):
    client = get_client_by_name(req.company_name)
    if not client:
        raise HTTPException(status_code=404, detail="client not found")
    # run synchronous generator in threadpool to avoid blocking
    loop = asyncio.get_running_loop()
    try:
        md = await loop.run_in_executor(None, generate_policy_for_client, req.company_name, req.language, req.custom_prompt)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"markdown": md}

# ========== Admin Authentication ==========
class LoginRequest(BaseModel):
    email: str
    password: str

@app.post("/api/v1/admin/login")
async def admin_login(req: LoginRequest):
    """Admin login endpoint (returns JWT)."""
    from db_utils import get_admin_by_email, update_admin_last_login

    admin = get_admin_by_email(req.email)
    if not admin or not admin.get("is_active"):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Verify password (bcrypt)
    try:
        if not bcrypt.checkpw(req.password.encode(), admin["password_hash"].encode()):
            raise HTTPException(status_code=401, detail="Invalid credentials")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Update last login
    update_admin_last_login(admin["id"])

    token = create_access_token(admin["id"], admin.get("role", "admin"))
    return {
        "id": admin["id"],
        "email": admin["email"],
        "full_name": admin.get("full_name"),
        "role": admin.get("role", "admin"),
        "token": token
    }

# ========== Master Prompts (Admin Only) ==========
class MasterPromptRequest(BaseModel):
    name: str
    prompt_text: str
    description: Optional[str] = None
    category: Optional[str] = None

class MasterPromptUpdate(BaseModel):
    """Model for updating an existing master prompt"""
    name: Optional[str] = None
    prompt_text: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    is_active: Optional[bool] = None

@app.get("/api/v1/master-prompts", dependencies=[Depends(require_api_key)])
async def get_master_prompts(is_active: Optional[bool] = None):
    """Get all master prompts (admin only). Filter by is_active if provided."""
    from db_utils import list_master_prompts
    # If is_active is None, fetch all prompts regardless of status
    if is_active is None:
        return list_master_prompts(is_active_only=False)
    return list_master_prompts(is_active_only=is_active)

@app.post("/api/v1/master-prompts", dependencies=[Depends(require_api_key)])
async def create_new_master_prompt(req: MasterPromptRequest):
    """Create a new master prompt (admin only)"""
    from db_utils import create_master_prompt
    try:
        prompt = create_master_prompt(
            name=req.name,
            prompt_text=req.prompt_text,
            description=req.description,
            category=req.category
        )
        return prompt
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/v1/master-prompts/{prompt_id}", dependencies=[Depends(require_api_key)])
async def get_master_prompt(prompt_id: str):
    """Get a specific master prompt by ID"""
    from db_utils import get_master_prompt_by_id
    prompt = get_master_prompt_by_id(prompt_id)
    if not prompt:
        raise HTTPException(status_code=404, detail="Master prompt not found")
    return prompt

@app.put("/api/v1/master-prompts/{prompt_id}", dependencies=[Depends(require_api_key)])
async def update_master_prompt_endpoint(prompt_id: str, updates: MasterPromptUpdate):
    """Update an existing master prompt (admin only)"""
    from db_utils import update_master_prompt, get_master_prompt_by_id
    
    # Check if prompt exists
    existing = get_master_prompt_by_id(prompt_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Master prompt not found")
    
    try:
        # Build update dict from provided fields only
        update_data = {}
        if updates.name is not None:
            update_data["name"] = updates.name
        if updates.prompt_text is not None:
            update_data["prompt_text"] = updates.prompt_text
        if updates.description is not None:
            update_data["description"] = updates.description
        if updates.category is not None:
            update_data["category"] = updates.category
        if updates.is_active is not None:
            update_data["is_active"] = updates.is_active
        
        if not update_data:
            raise HTTPException(status_code=400, detail="No fields provided to update")
        
        updated_prompt = update_master_prompt(prompt_id, **update_data)
        return updated_prompt
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/v1/master-prompts/{prompt_id}", dependencies=[Depends(require_api_key)])
async def delete_master_prompt_endpoint(prompt_id: str):
    """Delete (soft delete by setting is_active=false) a master prompt"""
    from db_utils import update_master_prompt, get_master_prompt_by_id
    
    existing = get_master_prompt_by_id(prompt_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Master prompt not found")
    
    try:
        # Soft delete by setting is_active to false
        update_master_prompt(prompt_id, is_active=False)
        return {"ok": True, "message": "Master prompt deactivated"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/policies", dependencies=[Depends(require_api_key)])
async def list_policies():
    # import your db helper here to avoid circular import at module load if needed
    from db_utils import list_policies as db_list_policies
    return db_list_policies(None)

# ========== Policies CRUD & Assignment ==========
from pydantic import BaseModel
from typing import Optional

class PolicyRequest(BaseModel):
    client_id: str
    title: str
    content: Optional[str] = None
    markdown: Optional[str] = None
    master_prompt_id: Optional[str] = None
    language: str = "en"
    status: str = "draft"

@app.post("/api/v1/policies", dependencies=[Depends(require_api_key)])
async def create_new_policy(req: PolicyRequest):
    """Create a new policy"""
    from db_utils import create_policy
    try:
        policy = create_policy(
            client_id=req.client_id,
            title=req.title,
            content=req.content,
            markdown=req.markdown,
            master_prompt_id=req.master_prompt_id,
            language=req.language,
            status=req.status
        )
        return policy
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/v1/policies/{policy_id}", dependencies=[Depends(require_api_key)])
async def get_policy(policy_id: str):
    """Get a specific policy by ID"""
    from db_utils import get_policy_by_id
    policy = get_policy_by_id(policy_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")
    return policy

@app.post("/api/v1/clients/{client_id}/policies/{policy_id}", dependencies=[Depends(require_api_key)])
async def assign_policy(client_id: str, policy_id: str):
    """Assign a policy to a client"""
    from db_utils import assign_policy_to_client
    try:
        result = assign_policy_to_client(client_id, policy_id)
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/v1/clients", dependencies=[Depends(require_api_key)])
async def list_clients():
    """Get all clients"""
    try:
        result = sb.table("clients").select("*").execute()
        return result.data if result.data else []
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Add admin-prefixed alias for frontend compatibility
@app.get("/api/v1/admin/clients", dependencies=[Depends(require_api_key)])
async def list_clients_admin():
    """Get all clients (admin route)"""
    try:
        result = sb.table("clients").select("*").execute()
        return result.data if result.data else []
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/clients", dependencies=[Depends(require_api_key)])
async def api_add_client(payload: dict):
    """
    Add a client. If the client already exists, return 409 with existing client id.
    """
    name = (payload.get("company_name") or "").strip()
    prov = payload.get("province", "N/A")
    lang = payload.get("language", "en")
    if not name:
        raise HTTPException(status_code=400, detail="company_name required")

    # Check for existing client to avoid unique constraint errors
    try:
        existing = sb.table("clients").select("id,company_name").eq("company_name", name).limit(1).execute()
        if existing and getattr(existing, "data", None):
            return JSONResponse(status_code=409, content={
                "detail": "client already exists",
                "client_id": existing.data[0].get("id"),
                "company_name": existing.data[0].get("company_name")
            })
    except Exception:
        pass

    try:
        add_res = sb.table("clients").insert({"company_name": name, "province": prov, "language": lang}).execute()
    except APIError as e:
        err_obj = {}
        try:
            err_obj = e.args[0] if e.args else {"message": str(e)}
        except Exception:
            err_obj = {"message": str(e)}
        if isinstance(err_obj, dict) and "duplicate key" in str(err_obj.get("message", "")).lower():
            return JSONResponse(status_code=409, content={"detail": "duplicate client", "db_error": err_obj})
        raise HTTPException(status_code=500, detail=f"Database error: {err_obj}")

    return {"ok": True, "result": add_res.data if hasattr(add_res, "data") else None}

@app.get("/api/v1/admin/regulations", dependencies=[Depends(require_api_key)])
async def list_regulations():
    """Get all regulations"""
    try:
        result = sb.table("regulations").select("*").execute()
        return result.data if result.data else []
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/admin/profile/{tenant_id}", dependencies=[Depends(require_api_key)])
async def get_tenant_profile(tenant_id: str):
    """Get tenant/client profile by ID"""
    try:
        result = sb.table("clients").select("*").eq("id", tenant_id).limit(1).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Tenant not found")
        return result.data[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ========== Onboarding & Profile Management ==========

class OnboardingData(BaseModel):
    """Questionnaire data structure matching frontend types"""
    company_legal_name: Optional[str] = None
    operating_name: Optional[str] = None
    business_number: Optional[str] = None
    incorporation_date: Optional[str] = None
    jurisdiction_incorporation: Optional[str] = None
    principal_address: Optional[str] = None
    mailing_address: Optional[str] = None
    business_phone: Optional[str] = None
    business_email: Optional[str] = None
    website: Optional[str] = None
    is_msb_registered: Optional[bool] = None
    fintrac_reg_number: Optional[str] = None
    msb_registration_date: Optional[str] = None
    jurisdictions: Optional[list[str]] = None
    msb_activities: Optional[list[str]] = None
    has_agents: Optional[bool] = None
    num_agents: Optional[int] = None
    agent_locations: Optional[str] = None
    compliance_officer_name: Optional[str] = None
    compliance_officer_email: Optional[str] = None
    compliance_officer_phone: Optional[str] = None
    aml_program_exists: Optional[bool] = None
    last_risk_assessment_date: Optional[str] = None
    high_risk_countries: Optional[bool] = None
    pep_dealings: Optional[bool] = None
    cash_intensive: Optional[bool] = None
    virtual_currency: Optional[bool] = None
    international_wires: Optional[bool] = None
    third_party_processors: Optional[bool] = None
    customer_types: Optional[list[str]] = None
    avg_transaction_volume: Optional[str] = None
    monthly_transaction_count: Optional[str] = None
    largest_transaction: Optional[str] = None
    existing_policies: Optional[list[str]] = None
    policy_update_frequency: Optional[str] = None
    training_frequency: Optional[str] = None
    record_keeping_system: Optional[str] = None
    reporting_mechanism: Optional[str] = None
    past_regulatory_issues: Optional[bool] = None
    regulatory_issue_details: Optional[str] = None
    additional_notes: Optional[str] = None

class CompanyProfileUpdate(BaseModel):
    """Full profile update including onboarding data"""
    company_name: Optional[str] = None
    province: Optional[str] = None
    language: Optional[str] = None
    onboarding_data: Optional[OnboardingData] = None

@app.get("/api/v1/clients/{tenant_id}", dependencies=[Depends(require_api_key)])
async def get_client_profile(tenant_id: str):
    """Get full client profile including onboarding data"""
    try:
        result = sb.table("clients").select("*").eq("id", tenant_id).limit(1).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Client not found")
        return result.data[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/v1/clients/{tenant_id}", dependencies=[Depends(require_api_key)])
async def update_client_profile(tenant_id: str, profile: CompanyProfileUpdate):
    """Update client profile including onboarding questionnaire data"""
    try:
        # Build update dict from provided fields
        update_data = {}
        if profile.company_name is not None:
            update_data["company_name"] = profile.company_name
        if profile.province is not None:
            update_data["province"] = profile.province
        if profile.language is not None:
            update_data["language"] = profile.language
        if profile.onboarding_data is not None:
            # Store onboarding data as JSONB
            update_data["onboarding_data"] = profile.onboarding_data.model_dump(exclude_none=True)
        
        update_data["updated_at"] = datetime.utcnow().isoformat()
        
        result = sb.table("clients").update(update_data).eq("id", tenant_id).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Client not found")
        return result.data[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/clients/{tenant_id}/onboarding", dependencies=[Depends(require_api_key)])
async def save_onboarding_data(tenant_id: str, data: OnboardingData):
    """Save or update onboarding questionnaire data for a client"""
    try:
        update_data = {
            "onboarding_data": data.model_dump(exclude_none=True),
            "updated_at": datetime.utcnow().isoformat()
        }
        
        result = sb.table("clients").update(update_data).eq("id", tenant_id).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Client not found")
        return {"ok": True, "data": result.data[0]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/clients/{tenant_id}/onboarding", dependencies=[Depends(require_api_key)])
async def get_onboarding_data(tenant_id: str):
    """Get onboarding questionnaire data for a client"""
    try:
        result = sb.table("clients").select("id,company_name,onboarding_data").eq("id", tenant_id).limit(1).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Client not found")
        return result.data[0]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Remove or replace the previous generic OPTIONS handler with this debug-friendly one.
# If CORSMiddleware is configured correctly you can delete this route entirely.
@app.options("/{full_path:path}")
async def _preflight_handler(full_path: str, request: Request):
    """
    Debug-friendly preflight handler: logs incoming preflight headers and returns a permissive CORS response.
    Temporary for dev only — consider removing and relying on CORSMiddleware in production.
    """
    # Log incoming preflight headers for debugging (do not log secrets in production)
    try:
        hdrs = {k.lower(): v for k, v in request.headers.items()}
        print(f"Preflight for: /{full_path} | Headers:", {k: hdrs.get(k) for k in ["origin","access-control-request-method","access-control-request-headers","host"]})
    except Exception:
        pass

    origin = request.headers.get("origin", "*")
    req_headers = request.headers.get("access-control-request-headers", "*")
    return Response(status_code=200, headers={
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        "Access-Control-Allow-Headers": req_headers,
        "Access-Control-Allow-Credentials": "true",
        # helpful for debugging
        "Access-Control-Max-Age": "600"
    })

# Add this middleware right after CORS middleware to log all incoming requests
@app.middleware("http")
async def log_requests(request: Request, call_next):
    print(f"📨 {request.method} {request.url.path} | Headers: {dict(request.headers)}")
    try:
        response = await call_next(request)
        print(f"{request.method} {request.url.path} -> {response.status_code}")
        return response
    except Exception as e:
        print(f" {request.method} {request.url.path} -> ERROR: {e}")
        raise