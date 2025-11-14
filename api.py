from fastapi import FastAPI, HTTPException, Request, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import asyncio
from policy_gen import generate_policy_for_client, get_client_by_name  # existing functions
from typing import Optional
import bcrypt
import jwt
from datetime import datetime, timedelta

API_KEY = os.getenv("API_KEY", "dev-key")  # set strong key in prod

app = FastAPI(title="Compl.AI API")

# Configure CORS for Google AI Studio origin(s)
origins = [
    "https://studio.googleapis.com",
    "http://localhost:3000",
    # add your front-end origin(s)
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins if os.getenv("ENV") == "prod" else ["*"],  # dev: allow all
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

def require_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
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

class AdminResponse(BaseModel):
    id: str
    email: str
    full_name: str
    role: str

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "your-secret-key-change-in-prod")

def create_access_token(admin_id: str, role: str) -> str:
    """Create a short-lived JWT for admin sessions."""
    payload = {
        "sub": admin_id,
        "role": role,
        "exp": datetime.utcnow() + timedelta(hours=24)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")

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

@app.get("/api/v1/master-prompts", dependencies=[Depends(require_api_key)])
async def get_master_prompts(is_active: bool = True):
    """Get all master prompts (admin only)"""
    from db_utils import list_master_prompts
    return list_master_prompts(is_active)

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