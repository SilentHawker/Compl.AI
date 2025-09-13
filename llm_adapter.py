from typing import Optional, Dict, Any
import os
import requests
import json
from dotenv import load_dotenv

load_dotenv()

# Optional token handling (for truncation helper)
try:
    import tiktoken
    _HAS_TIKTOKEN = True
except Exception:
    _HAS_TIKTOKEN = False


class LLMAdapter:
    """
    Centralized LLM adapter supporting: Gemini, OpenAI, Ollama.
    Select provider via env: LLM_PROVIDER = 'gemini' | 'openai' | 'ollama'
    Model envs:
      - Gemini: LLM_MODEL (e.g., 'gemini-1.5-flash' or 'gemini-2.0-flash')
      - OpenAI: OPENAI_MODEL (e.g., 'gpt-4o-mini')
      - Ollama: OLLAMA_MODEL (e.g., 'llama3:8b-instruct')
    """

    def __init__(self, provider: Optional[str] = None):
        self.provider = (provider or os.getenv("LLM_PROVIDER", "gemini")).lower()

        # Gemini
        self.gemini_key = os.getenv("GEMINI_API_KEY")
        self.gemini_model = os.getenv("LLM_MODEL", "gemini-1.5-flash")
        self.gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.gemini_model}:generateContent"

        # OpenAI
        self.openai_key = os.getenv("OPENAI_API_KEY")
        self.openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

        # Ollama
        self.ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
        self.ollama_model = os.getenv("OLLAMA_MODEL", "llama3:8b-instruct")

    # ---------- Utilities ----------
    def _truncate(self, text: str, max_tokens: int, model: Optional[str] = None) -> str:
        """Best-effort truncation by tokens; falls back to word count."""
        if _HAS_TIKTOKEN and model:
            try:
                enc = tiktoken.encoding_for_model(model)
                toks = enc.encode(text)
                if len(toks) <= max_tokens:
                    return text
                return enc.decode(toks[:max_tokens])
            except Exception:
                pass
        words = text.split()
        approx = max(10, int(max_tokens * 0.75))  # crude heuristic
        return " ".join(words[:approx])

    # ---------- Public APIs ----------
    def generate_text(self, prompt: str, max_output_tokens: int = 512, temperature: float = 0.0) -> Dict[str, Any]:
        """
        Uniform "text-in → raw JSON out" call. Use text_for() to extract final text.
        """
        if self.provider == "gemini":
            return self._call_gemini(prompt, max_output_tokens, temperature)
        elif self.provider == "openai":
            return self._call_openai(prompt, max_output_tokens, temperature)
        elif self.provider == "ollama":
            return self._call_ollama(prompt, max_output_tokens, temperature)
        else:
            raise RuntimeError(f"Unsupported LLM provider: {self.provider}")

    def chat_json(self, system_prompt: str, user_prompt: str, max_output_tokens: int = 1200, temperature: float = 0.0) -> Dict[str, Any]:
        """
        Ask model to return STRICT JSON. This builds the right payload per provider and parses JSON.
        """
        if self.provider == "gemini":
            if not self.gemini_key:
                raise RuntimeError("GEMINI_API_KEY not set")
            headers = {"Content-Type": "application/json", "X-goog-api-key": self.gemini_key}
            payload = {
                "systemInstruction": {"role": "system", "parts": [{"text": system_prompt}]},
                "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                "generationConfig": {
                    "temperature": float(temperature),
                    "maxOutputTokens": int(max_output_tokens),
                    "candidateCount": 1
                }
            }
            r = requests.post(self.gemini_url, headers=headers, json=payload, timeout=120)
            try:
                r.raise_for_status()
            except requests.HTTPError as e:
                raise RuntimeError(f"Gemini API request failed: {r.status_code} - {r.text}") from e
            data = r.json()
            try:
                txt = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(txt)
            except Exception:
                # Return raw for debugging rather than throwing
                return {"raw": data}

        elif self.provider == "openai":
            try:
                from openai import OpenAI
            except Exception:
                raise RuntimeError("openai package not installed")
            if not self.openai_key:
                raise RuntimeError("OPENAI_API_KEY not set")
            client = OpenAI(api_key=self.openai_key)
            resp = client.chat.completions.create(
                model=self.openai_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
                max_tokens=max_output_tokens,
                response_format={"type": "json_object"}
            )
            # Convert SDK object to dict
            return json.loads(resp.model_dump_json())

        elif self.provider == "ollama":
            # With Ollama you can't force strict JSON reliably — we do best effort and attempt to parse.
            url = f"{self.ollama_url}/api/chat"
            body = {
                "model": self.ollama_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "options": {"temperature": float(temperature)},
                "stream": False
            }
            r = requests.post(url, json=body, timeout=120)
            try:
                r.raise_for_status()
            except requests.HTTPError as e:
                raise RuntimeError(f"Ollama chat failed: {r.status_code} - {r.text}") from e
            data = r.json()
            # Extract text from final message
            try:
                txt = data["message"]["content"]
                return json.loads(txt)
            except Exception:
                return {"raw": data}

        else:
            raise RuntimeError(f"Unsupported LLM provider: {self.provider}")

    # ---------- Providers ----------
    # Gemini
        # ---------- Providers ----------
    # Gemini
    def _call_gemini(self, prompt: str, max_output_tokens: int, temperature: float) -> Dict[str, Any]:
        if not self.gemini_key:
            raise RuntimeError("GEMINI_API_KEY not set in environment")

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.gemini_model}:generateContent"
        headers = {"Content-Type": "application/json", "X-goog-api-key": self.gemini_key}

        use_minimal = str(os.getenv("GEMINI_MINIMAL", "0")).lower() in ("1", "true", "yes")
        if use_minimal:
            # Minimal payload, same as your curl
            payload = {
                "contents": [
                    {"parts": [{"text": prompt}]}
                ]
            }
        else:
            # Rich payload with generationConfig (camelCase keys!)
            payload = {
                "contents": [
                    {"role": "user", "parts": [{"text": prompt}]}
                ],
                "generationConfig": {
                    "temperature": float(temperature),
                    "maxOutputTokens": int(max_output_tokens),
                    "candidateCount": 1
                }
            }

        resp = requests.post(url, headers=headers, json=payload, timeout=120)
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            raise RuntimeError(f"Gemini API request failed: {resp.status_code} - {resp.text}") from e
        return resp.json()

    def chat_json(self, system_prompt: str, user_prompt: str, max_output_tokens: int = 1200, temperature: float = 0.0) -> Dict[str, Any]:
        if self.provider != "gemini":
            # (keep your OpenAI/Ollama branches as-is)
            raise RuntimeError("chat_json currently implemented for Gemini only in this patch")

        if not self.gemini_key:
            raise RuntimeError("GEMINI_API_KEY not set")

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.gemini_model}:generateContent"
        headers = {"Content-Type": "application/json", "X-goog-api-key": self.gemini_key}

        use_minimal = str(os.getenv("GEMINI_MINIMAL", "0")).lower() in ("1", "true", "yes")
        if use_minimal:
            # Minimal: prepend system text into the user content to keep it simple
            payload = {
                "contents": [
                    {"parts": [{"text": f"[SYSTEM]\n{system_prompt}\n\n[USER]\n{user_prompt}"}]}
                ]
            }
        else:
            payload = {
                "systemInstruction": {"role": "system", "parts": [{"text": system_prompt}]},
                "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                "generationConfig": {
                    "temperature": float(temperature),
                    "maxOutputTokens": int(max_output_tokens),
                    "candidateCount": 1
                }
            }

        r = requests.post(url, headers=headers, json=payload, timeout=120)
        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            raise RuntimeError(f"Gemini API request failed: {r.status_code} - {r.text}") from e

        data = r.json()
        # Extract the first text part safely
        try:
            txt = data["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(txt)  # if you asked for JSON, this succeeds
        except Exception:
            # Return raw so callers can inspect failures
            return {"raw": data}

    # OpenAI
    def _call_openai(self, prompt: str, max_output_tokens: int, temperature: float) -> Dict[str, Any]:
        try:
            from openai import OpenAI
        except Exception:
            raise RuntimeError("openai package not installed")
        if not self.openai_key:
            raise RuntimeError("OPENAI_API_KEY not set in environment")
        client = OpenAI(api_key=self.openai_key)
        resp = client.chat.completions.create(
            model=self.openai_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=max_output_tokens
        )
        # Convert SDK object to plain dict so text_for() can read it
        return json.loads(resp.model_dump_json())

    # Ollama
    def _call_ollama(self, prompt: str, max_output_tokens: int, temperature: float) -> Dict[str, Any]:
        url = f"{self.ollama_url}/api/generate"
        body = {
            "model": self.ollama_model,
            "prompt": prompt,
            "options": {"temperature": float(temperature)},
            "stream": False
        }
        r = requests.post(url, json=body, timeout=120)
        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            raise RuntimeError(f"Ollama generate failed: {r.status_code} - {r.text}") from e
        data = r.json()
        # Normalize to a dict with 'output_text'
        if isinstance(data, dict) and "response" in data:
            return {"output_text": data["response"], **data}
        return data

    # ---------- Extraction ----------
    def text_for(self, resp: Any) -> str:
        if resp is None:
            return ""
        if isinstance(resp, dict):
            # Gemini typical shape
            try:
                return resp["candidates"][0]["content"]["parts"][0]["text"]
            except Exception:
                pass
            # OpenAI chat.completions
            try:
                return resp["choices"][0]["message"]["content"]
            except Exception:
                pass
            # Ollama normalize
            if "response" in resp:
                return resp["response"]
            # Fallback
            return json.dumps(resp)
        return str(resp)
    
    # ---------- Extraction ----------
    #def text_for(self, resp: Any) -> str:
        """
        Returns the best-effort text from provider raw response.
        """
        if resp is None:
            return ""

        if isinstance(resp, dict):
            # Gemini typical
            try:
                return resp["candidates"][0]["content"]["parts"][0]["text"]
            except Exception:
                pass
            # OpenAI chat.completions
            try:
                return resp["choices"][0]["message"]["content"]
            except Exception:
                pass
            # OpenAI Responses (if ever used)
            if "output_text" in resp:
                return resp["output_text"]
            # Ollama normalize above
            if "response" in resp:
                return resp["response"]
            # Some earlier normalizations
            if "contents" in resp:
                try:
                    return resp["contents"][0]["parts"][0].get("text", "")
                except Exception:
                    pass
            # Fallback: stringify
            return json.dumps(resp)

        # If SDK object slipped through, stringify
        return str(resp)

