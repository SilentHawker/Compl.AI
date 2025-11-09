from fastapi import FastAPI, HTTPException, Request, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import asyncio
from policy_gen import generate_policy_for_client, get_client_by_name  # existing functions

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

@app.get("/api/v1/policies", dependencies=[Depends(require_api_key)])
async def list_policies():
    # import your db helper here to avoid circular import at module load if needed
    from db_utils import list_policies as db_list_policies
    return db_list_policies(None)

# add other endpoints as needed (GET /policies/{id}, POST /regulations/upload, etc.)