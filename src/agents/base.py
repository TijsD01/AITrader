import os
import re
import json
import shutil
import asyncio
import logging
from typing import Dict, Any, Optional
from config.settings import settings

logger = logging.getLogger(__name__)

def merge_llm_report(defaults: Dict[str, Any], res: Any, schema: Dict[str, Any], primary: str) -> Dict[str, Any]:
    """
    Overlays valid keys of an LLM response on top of the deterministic fallback `defaults`.
    schema maps key -> ("enum", {values}) | ("int", lo, hi) | ("float", lo, hi) | "str" | "list".
    If the `primary` key is missing/invalid the defaults are returned untouched, so every key
    always exists with a sane type.
    """
    merged = dict(defaults)
    if not isinstance(res, dict):
        return merged

    def _coerce(rule, val):
        kind = rule[0] if isinstance(rule, tuple) else rule
        if kind == "enum":
            norm = str(val).strip().upper().replace(" ", "_") if val is not None else ""
            return norm if norm in rule[1] else None
        if kind in ("int", "float"):
            if isinstance(val, bool) or val is None:
                return None
            try:
                num = float(val)
            except (TypeError, ValueError):
                return None
            if num != num or num in (float("inf"), float("-inf")):
                return None
            num = max(rule[1], min(rule[2], num))
            return int(round(num)) if kind == "int" else num
        if kind == "str":
            return val if isinstance(val, str) and val.strip() else None
        if kind == "list":
            return [str(x) for x in val] if isinstance(val, (list, tuple)) else None
        return None

    if _coerce(schema[primary], res.get(primary)) is None:
        return merged
    for key, rule in schema.items():
        if key in res:
            val = _coerce(rule, res[key])
            if val is not None:
                merged[key] = val
    return merged


class LLMClient:
    def __init__(self):
        self.provider = settings.LLM_PROVIDER.lower()
        self.gemini_key = settings.GEMINI_API_KEY or os.getenv("GEMINI_API_KEY")
        self.openai_key = settings.OPENAI_API_KEY or os.getenv("OPENAI_API_KEY")
        self.agy_path = self._resolve_agy_path()

    def _resolve_agy_path(self) -> Optional[str]:
        if hasattr(settings, "AGY_CLI_PATH") and settings.AGY_CLI_PATH and os.path.exists(settings.AGY_CLI_PATH):
            return settings.AGY_CLI_PATH
        env_exe = os.environ.get("ANTIGRAVITY_AGENTAPI_EXE")
        if env_exe and os.path.exists(env_exe):
            return env_exe
        found = shutil.which("agy")
        if found:
            return found
        for candidate in ["/snap/antigravity-cli/current/bin/agy", "/snap/bin/agy", "/usr/local/bin/agy", "/usr/bin/agy"]:
            if os.path.exists(candidate):
                return candidate
        return None

    def has_active_key(self) -> bool:
        self.provider = settings.LLM_PROVIDER.lower()
        if self.provider == "antigravity":
            return bool(self._resolve_agy_path())
        if self.provider == "gemini" and self.gemini_key and len(self.gemini_key) > 5 and not self.gemini_key.startswith("your_"):
            return True
        if self.provider == "openai" and self.openai_key and len(self.openai_key) > 5 and not self.openai_key.startswith("your_"):
            return True
        return False

    @staticmethod
    def _extract_json(text: str) -> Optional[Dict[str, Any]]:
        """Safely parses JSON from LLM response, stripping markdown or preamble if present."""
        if not text:
            return None
        clean = text.strip()
        try:
            parsed = json.loads(clean)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            match = re.search(r"(\{.*\})", clean, re.DOTALL)
            if match:
                try:
                    parsed = json.loads(match.group(1))
                    return parsed if isinstance(parsed, dict) else None
                except Exception:
                    pass
        return None

    async def _call_antigravity(self, system_prompt: str, user_prompt: str) -> Optional[Dict[str, Any]]:
        """
        Invokes local Antigravity CLI (agy) using the user's logged-in
        Antigravity plan quota without requiring external API keys.
        """
        agy_bin = self._resolve_agy_path()
        if not agy_bin:
            logger.warning("Antigravity CLI (agy) binary not found on system.")
            return None

        prompt = (
            f"{system_prompt}\n\n"
            f"IMPORTANT: Respond ONLY with a valid, raw JSON object. Do not include introductory or concluding remarks.\n\n"
            f"{user_prompt}"
        )

        proc = None
        try:
            # shortcut: retries a crash of the spawned child before exec (rc<0), not the root Python/glibc spawn bug; upgrade when the crash is fixed at the source
            for attempt in range(3):
                proc = await asyncio.create_subprocess_exec(
                    agy_bin,
                    "--disable-slash-commands",
                    "--print",
                    prompt,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=90.0)

                if proc.returncode == 0:
                    break
                err_msg = stderr.decode(errors="replace")
                if proc.returncode < 0 and attempt < 2:
                    logger.warning(f"Antigravity CLI killed by signal {-proc.returncode} (attempt {attempt + 1}/3), retrying: {err_msg}")
                    await asyncio.sleep(1.0)
                    continue
                logger.error(f"Antigravity CLI returned exit code {proc.returncode}: {err_msg}")
                return None

            output_text = stdout.decode(errors="replace").strip()
            return self._extract_json(output_text)

        except asyncio.TimeoutError:
            logger.error("Antigravity CLI timed out after 90s.")
            await self._kill_proc(proc)
            return None
        except asyncio.CancelledError:
            await self._kill_proc(proc)
            raise
        except Exception as e:
            logger.error(f"Error calling Antigravity CLI: {e}")
            await self._kill_proc(proc)
            return None

    @staticmethod
    async def _kill_proc(proc) -> None:
        """Kills and reaps a still-running subprocess so it does not leak."""
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception as e:
            logger.debug(f"Error killing Antigravity CLI process: {e}")
        try:
            await asyncio.shield(proc.wait())
        except BaseException:
            pass

    async def generate_json(self, system_prompt: str, user_prompt: str) -> Optional[Dict[str, Any]]:
        """
        Calls Antigravity CLI, Google Gemini API, or OpenAI API to get structured JSON output.
        Returns parsed dict, or None to fall back to institutional quant models.
        """
        self.provider = settings.LLM_PROVIDER.lower()
        self.gemini_key = settings.GEMINI_API_KEY or os.getenv("GEMINI_API_KEY")
        self.openai_key = settings.OPENAI_API_KEY or os.getenv("OPENAI_API_KEY")

        # 1. Antigravity Agent Mode (No API keys needed!)
        if self.provider == "antigravity":
            result = await self._call_antigravity(system_prompt, user_prompt)
            if result:
                return result
            logger.info("Antigravity CLI returned no parseable JSON, falling back to quant model.")

        # 2. Gemini API Mode (if key provided)
        elif self.provider == "gemini" and self.gemini_key and not self.gemini_key.startswith("your_"):
            def _call_gemini_sync():
                from google import genai

                client = genai.Client(api_key=self.gemini_key)
                prompt = (
                    f"{system_prompt}\n\n"
                    f"Task:\n{user_prompt}\n\n"
                    f"IMPORTANT: Respond ONLY with a valid, raw JSON object. Do not include markdown fences or extraneous text."
                )
                models_to_try = [settings.GEMINI_MODEL]
                for fb in ["gemini-flash-latest", "gemini-2.5-flash", "gemini-3.7-flash"]:
                    if fb not in models_to_try:
                        models_to_try.append(fb)

                for target_model in models_to_try:
                    try:
                        res = client.models.generate_content(
                            model=target_model,
                            contents=prompt
                        )
                        text = getattr(res, "text", None)
                        if text:
                            return text
                    except Exception as ex_try:
                        logger.warning(f"Gemini model {target_model} temporary error ({ex_try}), trying fallback...")
                        continue
                return None

            try:
                text = await asyncio.wait_for(asyncio.to_thread(_call_gemini_sync), timeout=30.0)
                return self._extract_json(text)
            except asyncio.TimeoutError:
                logger.error("Gemini API call timed out after 30s.")
            except Exception as e:
                logger.error(f"Gemini API generation error: {e}")

        # 3. OpenAI Mode (if key provided)
        elif self.provider == "openai" and self.openai_key and not self.openai_key.startswith("your_"):
            try:
                import openai
                client = openai.AsyncOpenAI(api_key=self.openai_key)
                response = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=settings.OPENAI_MODEL,
                        messages=[
                            {"role": "system", "content": f"{system_prompt}\nYou must respond in pure JSON format."},
                            {"role": "user", "content": user_prompt}
                        ],
                        response_format={"type": "json_object"},
                        temperature=0.2
                    ),
                    timeout=25.0
                )
                content = response.choices[0].message.content
                return self._extract_json(content)
            except asyncio.TimeoutError:
                logger.error("OpenAI API call timed out after 25s.")
            except Exception as e:
                logger.error(f"OpenAI API generation error: {e}")

        return None

llm_client = LLMClient()
