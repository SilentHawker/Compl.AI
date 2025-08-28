import os, json
from openai import OpenAI
import requests

class LLMAdapter:
    def __init__(self):
        self.provider = os.getenv("LLM_PROVIDER", "openai").lower()  # 'openai' or 'ollama'
        self.model = os.getenv("LLM_MODEL", "gpt-4o-mini") if self.provider == "openai" else os.getenv("LLM_MODEL", "llama3:8b-instruct")
        if self.provider == "openai":
            self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        else:
            # Ensure: `ollama serve` is running and model is pulled: `ollama pull llama3:8b-instruct`
            self.ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat")

    def chat_json(self, system_prompt: str, user_prompt: str) -> dict:
        if self.provider == "openai":
            resp = self.client.chat.completions.create(
                model=self.model,
                temperature=0.1,
                response_format={"type":"json_object"},
                messages=[{"role":"system","content":system_prompt},{"role":"user","content":user_prompt}],
            )
            return json.loads(resp.choices[0].message.content)
        else:
            # Ollama chat endpoint expects messages; returns text, we’ll try to parse as JSON
            payload = {
                "model": self.model,
                "messages": [
                    {"role":"system","content":system_prompt},
                    {"role":"user","content":user_prompt}
                ],
                "options": {"temperature": 0.1}
            }
            r = requests.post(self.ollama_url, json=payload, timeout=600)
            r.raise_for_status()
            # Ollama may stream; the chat API returns final content in response.json()["message"]["content"]
            data = r.json()
            content = data.get("message", {}).get("content", "")
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                # best-effort repair: ask the model to fix JSON via OpenAI if available, else raise
                if os.getenv("OPENAI_API_KEY"):
                    fix = OpenAI(api_key=os.getenv("OPENAI_API_KEY")).chat.completions.create(
                        model="gpt-4o-mini",
                        temperature=0.0,
                        response_format={"type":"json_object"},
                        messages=[{"role":"system","content":"Fix the following into STRICT JSON only. No commentary."},
                                  {"role":"user","content":content}]
                    )
                    return json.loads(fix.choices[0].message.content)
                raise
