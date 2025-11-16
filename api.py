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
import asyncio
from policy_gen import generate_policy_for_client
from db_utils import get_client_by_name, sb
from typing import Optional, List
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

# Startup check for API_KEY
@app.on_event("startup")
def _check_api_key_present():
    v = (os.getenv("API_KEY") or "").strip().lstrip("\ufeff")
    print("API_KEY present in env:", bool(v))
    if not v:
        print("WARNING: API_KEY not set in environment (.env or env vars).")

def require_api_key(x_api_key: str = Header(...)):
    expected = (os.getenv("API_KEY") or "").strip().lstrip("\ufeff")
    if not expected:
        print("ERROR: API_KEY not found in environment (.env not loaded or missing).")
        raise HTTPException(status_code=500, detail="Server API key not configured")
    provided = (x_api_key or "").strip()
    if provided != expected:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key

# ========== Request/Response Models ==========

class GenerateRequest(BaseModel):
    company_name: str
    custom_prompt: str | None = None
    language: str | None = None

class GenerateResponse(BaseModel):
    markdown: str

class LoginRequest(BaseModel):
    email: str
    password: str

class ClientCreateRequest(BaseModel):
    company_name: str
    province: Optional[str] = "N/A"
    language: Optional[str] = "en"

class MasterPromptRequest(BaseModel):
    name: str
    prompt_text: str
    description: Optional[str] = None
    category: Optional[str] = None

class MasterPromptUpdate(BaseModel):
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

# ========== Health Check ==========

@app.get("/health")
async def health():
    return {"ok": True}

# ========== Authentication ==========

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

# ========== Clients Management ==========

@app.post("/clients", dependencies=[Depends(require_api_key)])
async def create_client(payload: ClientCreateRequest):
    """Create a new client/tenant account (Admin only)"""
    name = payload.company_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="company_name required")

    # Check for existing client
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
        add_res = sb.table("clients").insert({
            "company_name": name,
            "province": payload.province,
            "language": payload.language
        }).execute()
        return {"ok": True, "result": add_res.data[0] if hasattr(add_res, "data") and add_res.data else None}
    except APIError as e:
        err_obj = e.args[0] if e.args else {"message": str(e)}
        if isinstance(err_obj, dict) and "duplicate key" in str(err_obj.get("message", "")).lower():
            return JSONResponse(status_code=409, content={"detail": "duplicate client", "db_error": err_obj})
        raise HTTPException(status_code=500, detail=f"Database error: {err_obj}")

@app.get("/api/v1/clients", dependencies=[Depends(require_api_key)])
async def list_clients():
    """Get all clients"""
    try:
        result = sb.table("clients").select("*").execute()
        return result.data if result.data else []
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/admin/clients", dependencies=[Depends(require_api_key)])
async def list_clients_admin():
    """Get all clients (admin route for tenant switcher)"""
    try:
        result = sb.table("clients").select("id,company_name,created_at").execute()
        # Map to frontend expected format
        return [{"client_id": c.get("id"), "company_name": c.get("company_name")} for c in (result.data or [])]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/clients/{tenant_id}", dependencies=[Depends(require_api_key)])
async def get_client_profile(tenant_id: str):
    """Get full client profile including onboarding data"""
    try:
        result = sb.table("clients").select("*").eq("id", tenant_id).limit(1).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Client not found")
        
        # Map to frontend expected format
        client = result.data[0]
        return {
            "client_id": client.get("id"),
            "company_name": client.get("company_name"),
            "operating_name": client.get("operating_name"),
            "fintrac_reg_number": client.get("fintrac_reg_number"),
            "business_address": client.get("business_address"),
            "business_lines": client.get("business_lines", []),
            "employees": client.get("employees", []),
            "onboarding_data": client.get("onboarding_data", {})
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/v1/clients/{tenant_id}", dependencies=[Depends(require_api_key)])
async def update_client_profile(tenant_id: str, profile: CompanyProfileUpdate):
    """Update client profile including onboarding questionnaire data"""
    try:
        update_data = {}
        if profile.company_name is not None:
            update_data["company_name"] = profile.company_name
        if profile.province is not None:
            update_data["province"] = profile.province
        if profile.language is not None:
            update_data["language"] = profile.language
        if profile.onboarding_data is not None:
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

@app.get("/api/v1/admin/profile/{tenant_id}", dependencies=[Depends(require_api_key)])
async def get_tenant_profile(tenant_id: str):
    """Get tenant/client profile by ID (alias for backwards compatibility)"""
    return await get_client_profile(tenant_id)

# ========== Onboarding ==========

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

# ========== Policies ==========

@app.get("/api/v1/policies", dependencies=[Depends(require_api_key)])
async def list_policies():
    """Get all policies"""
    from db_utils import list_policies as db_list_policies
    return db_list_policies(None)

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

# ========== Policy Generation (AI) ==========

@app.post("/api/v1/generate", response_model=GenerateResponse, dependencies=[Depends(require_api_key)])
async def generate(req: GenerateRequest):
    """Generate a policy using AI"""
    client = get_client_by_name(req.company_name)
    if not client:
        raise HTTPException(status_code=404, detail="client not found")
    
    loop = asyncio.get_running_loop()
    try:
        md = await loop.run_in_executor(
            None,
            generate_policy_for_client,
            req.company_name,
            req.language,
            req.custom_prompt
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"markdown": md}

# ========== Master Prompts ==========

@app.get("/api/v1/master-prompts", dependencies=[Depends(require_api_key)])
async def get_master_prompts(is_active: Optional[bool] = None):
    """Get all master prompts with optional active filter"""
    from db_utils import list_master_prompts
    if is_active is None:
        return list_master_prompts(is_active_only=False)
    return list_master_prompts(is_active_only=is_active)

@app.post("/api/v1/master-prompts", dependencies=[Depends(require_api_key)])
async def create_new_master_prompt(req: MasterPromptRequest):
    """Create a new master prompt"""
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
    """Update an existing master prompt"""
    from db_utils import update_master_prompt, get_master_prompt_by_id
    
    existing = get_master_prompt_by_id(prompt_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Master prompt not found")
    
    try:
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
    """Soft delete a master prompt by setting is_active=false"""
    from db_utils import update_master_prompt, get_master_prompt_by_id
    
    existing = get_master_prompt_by_id(prompt_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Master prompt not found")
    
    try:
        update_master_prompt(prompt_id, is_active=False)
        return {"ok": True, "message": "Master prompt deactivated"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ========== Regulations/Sources ==========

@app.get("/api/v1/sources", dependencies=[Depends(require_api_key)])
async def list_regulations():
    """Get all regulatory sources"""
    try:
        result = sb.table("regulations").select("*").execute()
        # Map to frontend expected format
        regulations = []
        for reg in (result.data or []):
            regulations.append({
                "id": reg.get("id"),
                "name": reg.get("name"),
                "link": reg.get("link"),
                "interpretation": reg.get("interpretation"),
                "isVerified": reg.get("is_verified", True),
                "createdAt": reg.get("created_at"),
                "businessLine": reg.get("business_line")
            })
        return regulations
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Alias for backwards compatibility
@app.get("/api/v1/admin/regulations", dependencies=[Depends(require_api_key)])
async def list_regulations_admin():
    """Get all regulations (admin alias)"""
    return await list_regulations()

# ========== Business Lines ==========

class BusinessLineRequest(BaseModel):
    name: str

@app.get("/api/v1/business-lines", dependencies=[Depends(require_api_key)])
async def list_business_lines():
    """Get all business lines"""
    try:
        result = sb.table("business_lines").select("*").order("name").execute()
        return result.data if result.data else []
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/business-lines", dependencies=[Depends(require_api_key)])
async def create_business_line(req: BusinessLineRequest):
    """Create a new business line"""
    try:
        result = sb.table("business_lines").insert({"name": req.name.strip()}).execute()
        if not result.data:
            raise HTTPException(status_code=500, detail="Failed to create business line")
        return result.data[0]
    except APIError as e:
        err_msg = str(e)
        if "duplicate key" in err_msg.lower() or "unique" in err_msg.lower():
            raise HTTPException(status_code=409, detail="Business line already exists")
        raise HTTPException(status_code=500, detail=err_msg)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/v1/business-lines/{business_line_id}", dependencies=[Depends(require_api_key)])
async def delete_business_line(business_line_id: str):
    """Delete a business line"""
    try:
        result = sb.table("business_lines").delete().eq("id", business_line_id).execute()
        return Response(status_code=204)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ========== Enhanced Regulations Management ==========

class RegulationRequest(BaseModel):
    name: str
    link: str
    interpretation: str
    business_lines: Optional[List[str]] = []

class RegulationUpdate(BaseModel):
    name: Optional[str] = None
    link: Optional[str] = None
    interpretation: Optional[str] = None
    business_lines: Optional[List[str]] = None
    status: Optional[str] = None
    status_message: Optional[str] = None

@app.get("/api/v1/regulations", dependencies=[Depends(require_api_key)])
async def list_all_regulations():
    """Get all regulations with full details"""
    try:
        result = sb.table("regulations").select("*").execute()
        regulations = []
        for reg in (result.data or []):
            regulations.append({
                "id": reg.get("id"),
                "name": reg.get("name"),
                "link": reg.get("link"),
                "interpretation": reg.get("interpretation"),
                "businessLines": reg.get("business_lines", []),
                "lastChecked": reg.get("last_checked"),
                "status": reg.get("status", "unchanged"),
                "statusMessage": reg.get("status_message"),
                "createdAt": reg.get("created_at")
            })
        return regulations
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/regulations", dependencies=[Depends(require_api_key)])
async def create_regulation(req: RegulationRequest):
    """Create a new regulation"""
    try:
        insert_data = {
            "name": req.name.strip(),
            "link": req.link.strip(),
            "interpretation": req.interpretation.strip(),
            "business_lines": req.business_lines,
            "status": "pending",
            "created_at": datetime.utcnow().isoformat()
        }
        result = sb.table("regulations").insert(insert_data).execute()
        if not result.data:
            raise HTTPException(status_code=500, detail="Failed to create regulation")
        
        reg = result.data[0]
        return {
            "id": reg.get("id"),
            "name": reg.get("name"),
            "link": reg.get("link"),
            "interpretation": reg.get("interpretation"),
            "businessLines": reg.get("business_lines", []),
            "lastChecked": reg.get("last_checked"),
            "status": reg.get("status", "pending"),
            "statusMessage": reg.get("status_message"),
            "createdAt": reg.get("created_at")
        }
    except APIError as e:
        err_msg = str(e)
        if "duplicate key" in err_msg.lower() or "unique" in err_msg.lower():
            raise HTTPException(status_code=409, detail="Regulation with this name or link already exists")
        raise HTTPException(status_code=500, detail=err_msg)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/v1/regulations/{regulation_id}", dependencies=[Depends(require_api_key)])
async def update_regulation(regulation_id: str, updates: RegulationUpdate):
    """Update an existing regulation"""
    try:
        # Check if exists
        existing = sb.table("regulations").select("id").eq("id", regulation_id).limit(1).execute()
        if not existing.data:
            raise HTTPException(status_code=404, detail="Regulation not found")
        
        # Build update dict
        update_data = {}
        if updates.name is not None:
            update_data["name"] = updates.name.strip()
        if updates.link is not None:
            update_data["link"] = updates.link.strip()
        if updates.interpretation is not None:
            update_data["interpretation"] = updates.interpretation.strip()
        if updates.business_lines is not None:
            update_data["business_lines"] = updates.business_lines
        if updates.status is not None:
            update_data["status"] = updates.status
        if updates.status_message is not None:
            update_data["status_message"] = updates.status_message
        
        if not update_data:
            raise HTTPException(status_code=400, detail="No fields provided to update")
        
        update_data["updated_at"] = datetime.utcnow().isoformat()
        
        result = sb.table("regulations").update(update_data).eq("id", regulation_id).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Regulation not found")
        
        reg = result.data[0]
        return {
            "id": reg.get("id"),
            "name": reg.get("name"),
            "link": reg.get("link"),
            "interpretation": reg.get("interpretation"),
            "businessLines": reg.get("business_lines", []),
            "lastChecked": reg.get("last_checked"),
            "status": reg.get("status"),
            "statusMessage": reg.get("status_message"),
            "createdAt": reg.get("created_at")
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/v1/regulations/{regulation_id}", dependencies=[Depends(require_api_key)])
async def delete_regulation(regulation_id: str):
    """Delete a regulation"""
    try:
        # Check if exists
        existing = sb.table("regulations").select("id").eq("id", regulation_id).limit(1).execute()
        if not existing.data:
            raise HTTPException(status_code=404, detail="Regulation not found")
        
        sb.table("regulations").delete().eq("id", regulation_id).execute()
        return Response(status_code=204)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/regulations/trigger-checks", dependencies=[Depends(require_api_key)])
async def trigger_regulation_checks():
    """Manually trigger AI checks for all regulations (async background job)"""
    try:
        # Get all regulations
        result = sb.table("regulations").select("*").execute()
        regulations = result.data if result.data else []
        
        # TODO: Implement actual AI checking logic here
        # For now, we'll just mark all as pending
        for reg in regulations:
            sb.table("regulations").update({
                "status": "pending",
                "last_checked": datetime.utcnow().isoformat()
            }).eq("id", reg["id"]).execute()
        
        return JSONResponse(
            status_code=202,
            content={
                "message": "Regulation checks triggered",
                "count": len(regulations),
                "timestamp": datetime.utcnow().isoformat()
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ========== Logging Middleware (Keep at end) ==========

@app.middleware("http")
async def log_requests(request: Request, call_next):
    print(f"📨 {request.method} {request.url.path}")
    try:
        response = await call_next(request)
        print(f"✅ {request.method} {request.url.path} -> {response.status_code}")
        return response
    except Exception as e:
        print(f"❌ {request.method} {request.url.path} -> ERROR: {e}")
        raise