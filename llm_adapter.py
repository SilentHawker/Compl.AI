import os
import time
import json
from typing import Optional, Dict, Any
import requests
from dotenv import load_dotenv

load_dotenv()

# optional high-fidelity token handling
try:
    import tiktoken
    _HAS_TIKTOKEN = True
except Exception:
    _HAS_TIKTOKEN = False

class LLMAdapter:
    """
    Minimal adapter for Gemini Flash 2 (gemini-2.0-flash) as the primary provider.
    Use LLM_PROVIDER env only if you later support other providers.
    """
    def __init__(self, provider: Optional[str] = None, model: Optional[str] = None):
        self.provider = (provider or os.getenv("LLM_PROVIDER", "gemini")).lower()
        # default to Gemini Flash 2
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
        self.gemini_key = os.getenv("GEMINI_API_KEY")
        if self.provider != "gemini":
            # keep adapter extensible, but default single-provider behavior is gemini
            self.provider = "gemini"

    def _encoding_for(self, model: Optional[str]):
        if not _HAS_TIKTOKEN or not model:
            return None
        try:
            return tiktoken.encoding_for_model(model)
        except Exception:
            return None

    def _truncate(self, text: str, max_tokens: int, model: Optional[str] = None) -> str:
        if not text:
            return ""
        enc = self._encoding_for(model)
        if enc:
            toks = enc.encode(text)
            if len(toks) <= max_tokens:
                return text
            return enc.decode(toks[:max_tokens])
        # fallback: conservative word-based cut
        words = text.split()
        approx_words = max(10, int(max_tokens * 0.75))
        return " ".join(words[:approx_words])

    def generate_text(self, prompt: str, max_output_tokens: int = 512, temperature: float = 0.0, retry: int = 3, timeout: int = 60) -> Dict[str, Any]:
        """
        Returns provider raw JSON response. Default provider is Gemini Flash 2.
        """
        if self.provider == "gemini":
            return self._call_gemini(prompt, max_output_tokens, temperature, retry=retry, timeout=timeout)
        raise RuntimeError(f"Unsupported LLM provider: {self.provider}")

    def _call_gemini(self, prompt: str, max_output_tokens: int, temperature: float, retry: int = 3, timeout: int = 60) -> Dict[str, Any]:
        if not self.gemini_key:
            raise RuntimeError("GEMINI_API_KEY not set in environment")
        model = self.model
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        headers = {
            "Content-Type": "application/json",
            "X-goog-api-key": self.gemini_key
        }
        payload = {
            "contents": [
                {"parts": [{"text": prompt}]}
            ],
            "generationConfig": {
                "maxOutputTokens": int(max_output_tokens),
                "temperature": float(temperature),
                "candidateCount": 1
            }
        }

        last_exc = None
        for attempt in range(1, retry + 1):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
                if resp.status_code >= 500 or resp.status_code == 429:
                    last_exc = RuntimeError(f"Transient LLM error {resp.status_code}: {resp.text}")
                    time.sleep(1.5 ** attempt)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.HTTPError as e:
                # surface response body for debugging on final attempt
                last_exc = e
                if attempt == retry:
                    body = resp.text if resp is not None else str(e)
                    raise RuntimeError(f"Gemini API request failed: {resp.status_code} - {body}") from e
                time.sleep(1.5 ** attempt)
            except Exception as e:
                last_exc = e
                if attempt == retry:
                    raise RuntimeError(f"Gemini API call error: {e}") from e
                time.sleep(1.5 ** attempt)
        raise last_exc or RuntimeError("Gemini call failed")

    def text_for(self, resp: Any) -> str:
        """Extract human-readable text from common Gemini response shapes."""
        if resp is None:
            return ""
        # If response is a requests-like object dict
        if isinstance(resp, dict):
            # new Gemini shapes: try 'candidates' -> 'content' or 'output'/'outputs'
            if "candidates" in resp:
                try:
                    c0 = resp["candidates"][0]
                    # candidate may contain 'content' or 'output'
                    if isinstance(c0, dict):
                        return c0.get("content") or c0.get("output") or json.dumps(c0)
                except Exception:
                    pass
            # legacy / other shapes: 'contents' with parts
            if "contents" in resp:
                try:
                    return resp["contents"][0]["parts"][0].get("text", "")
                except Exception:
                    pass
            if "outputs" in resp:
                try:
                    out0 = resp["outputs"][0]
                    if isinstance(out0, dict):
                        # sometimes 'content' nested
                        return out0.get("content") or json.dumps(out0)
                except Exception:
                    pass
            # openai-like choices
            if "choices" in resp:
                try:
                    c = resp["choices"][0]
                    return c.get("message", {}).get("content") or c.get("text") or json.dumps(c)
                except Exception:
                    pass
            # fallback stringify
            try:
                return json.dumps(resp)
            except Exception:
                return str(resp)
        if isinstance(resp, str):
            return resp

