from typing import Optional, Dict, Any
import os
import requests
import json
from dotenv import load_dotenv

load_dotenv()

# Optional token handling
try:
    import tiktoken
    _HAS_TIKTOKEN = True
except Exception:
    _HAS_TIKTOKEN = False

class LLMAdapter:
    """
    Simple adapter to centralize LLM calls. Default provider is GEMINI.
    Set LLM_PROVIDER in .env to 'gemini', 'openai' or 'ollama'.
    """
    def __init__(self, provider: Optional[str] = None):
        self.provider = (provider or os.getenv("LLM_PROVIDER", "gemini")).lower()
        self.gemini_key = os.getenv("GEMINI_API_KEY")
        self.openai_key = os.getenv("OPENAI_API_KEY")
        self.ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")  # change if needed

    def _truncate(self, text: str, max_tokens: int, model: Optional[str] = None) -> str:
        if _HAS_TIKTOKEN and model:
            try:
                enc = tiktoken.encoding_for_model(model)
                toks = enc.encode(text)
                if len(toks) <= max_tokens:
                    return text
                return enc.decode(toks[:max_tokens])
            except Exception:
                pass
        # fallback: naive truncation by words
        words = text.split()
        approx = max(10, max_tokens * 2)  # crude heuristic
        return " ".join(words[:approx])

    def generate_text(self, prompt: str, max_output_tokens: int = 512, temperature: float = 0.0) -> Dict[str, Any]:
        """
        Returns provider raw JSON response. Consumer should extract text as needed.
        """
        if self.provider == "gemini":
            return self._call_gemini(prompt, max_output_tokens, temperature)
        if self.provider == "openai":
            return self._call_openai(prompt, max_output_tokens, temperature)
        if self.provider == "ollama":
            return self._call_ollama(prompt, max_output_tokens, temperature)
        raise RuntimeError(f"Unsupported LLM provider: {self.provider}")

    # ---- Gemini ----
    def _call_gemini(self, prompt: str, max_output_tokens: int, temperature: float) -> Dict[str, Any]:
        if not self.gemini_key:
            raise RuntimeError("GEMINI_API_KEY not set in environment")
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
        headers = {
            "Content-Type": "application/json",
            "X-goog-api-key": self.gemini_key
        }
        payload = {
            "contents": [
                {"parts": [{"text": prompt}]}
            ],
            "temperature": temperature,
            "candidate_count": 1,
            "max_output_tokens": max_output_tokens
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        return resp.json()

    # ---- OpenAI (uses openai-python if available) ----
    def _call_openai(self, prompt: str, max_output_tokens: int, temperature: float) -> Dict[str, Any]:
        try:
            from openai import OpenAI
        except Exception:
            raise RuntimeError("openai package not installed or not importable")
        if not self.openai_key:
            raise RuntimeError("OPENAI_API_KEY not set in environment")
        client = OpenAI(api_key=self.openai_key)
        # using text generation endpoint; adjust for specific OpenAI client if needed
        resp = client.responses.create(model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                                       input=prompt,
                                       max_output_tokens=max_output_tokens,
                                       temperature=temperature)
        # return raw response
        return resp

    # ---- Ollama (local REST) ----
    def _call_ollama(self, prompt: str, max_output_tokens: int, temperature: float) -> Dict[str, Any]:
        url = f"{self.ollama_url}/api/generate"
        body = {
            "model": os.getenv("OLLAMA_MODEL", "llama2"),
            "prompt": prompt,
            "max_tokens": max_output_tokens,
            "temperature": temperature
        }
        resp = requests.post(url, json=body, timeout=60)
        resp.raise_for_status()
        return resp.json()

    # Convenience: return best text string (tries common response shapes)
    def text_for(self, resp: Any) -> str:
        if resp is None:
            return ""
        if isinstance(resp, dict):
            # Gemini-like: look for 'candidates' or 'outputs' or 'contents'
            # Try several common shapes
            if "candidates" in resp:
                try:
                    return resp["candidates"][0].get("content", "")
                except Exception:
                    pass
            if "output" in resp:
                return json.dumps(resp["output"]) if not isinstance(resp["output"], str) else resp["output"]
            if "contents" in resp:
                try:
                    return resp["contents"][0]["parts"][0].get("text", "")
                except Exception:
                    pass
            # OpenAI responses may be objects with 'output' or 'choices'
            if "choices" in resp:
                try:
                    c = resp["choices"][0]
                    return c.get("message", {}).get("content") or c.get("text") or ""
                except Exception:
                    pass
            # fallback: stringify
            return json.dumps(resp)
        if isinstance(resp, str):
            return resp
        return str(resp)
