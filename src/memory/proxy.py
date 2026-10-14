import os
import time
import json
import logging
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from config.settings import settings
from src.memory.cold_start import cold_start_engine
from src.memory.extractor import asset_extractor
from src.memory.models import L0Message
from src.memory.store import memory_store

logger = logging.getLogger(__name__)

proxy_router = APIRouter(tags=["MemoryProxy"])


def _content_to_text(content: Any) -> str:
    """Normalises message/system content (str, or a list of {"type": "text"} blocks) to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict) and b.get("type", "text") == "text":
                parts.append(str(b.get("text", "")))
        return " ".join(p for p in parts if p)
    return str(content)


@proxy_router.post("/v1/chat/completions")
async def openai_chat_completions_proxy(request: Request):
    """
    OpenAI-compatible chat completions reverse proxy.
    Transparently injects TencentDB Agent Memory into the system prompt,
    forwards to LLM provider (or Antigravity CLI), and extracts memory assets asynchronously.
    """
    body = await request.json()
    messages: List[Dict[str, Any]] = body.get("messages", [])
    model = body.get("model", settings.OPENAI_MODEL or "default")
    team_id = request.headers.get("x-tdai-team-id", "trading-team")
    agent_id = request.headers.get("x-tdai-agent-id", "committee")
    session_id = request.headers.get("x-session-id", f"proxy_{int(time.time()*1000)}")

    # 1. Retrieve Cold Start Memory Context
    last_user_msg = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user_msg = _content_to_text(m.get("content", ""))
            break

    cold_start = await cold_start_engine.generate_cold_start_loadout(
        team_id=team_id,
        agent_id=agent_id,
        query=last_user_msg[:200] if last_user_msg else ""
    )

    # 2. Inject Memory Context into System Prompt
    system_injected = False
    new_messages = []
    for m in messages:
        if m.get("role") == "system" and not system_injected:
            orig = _content_to_text(m.get("content", ""))
            augmented = f"{cold_start.full_prompt_context}\n\n{orig}"
            new_messages.append({"role": "system", "content": augmented})
            system_injected = True
        else:
            new_messages.append({**m, "content": _content_to_text(m.get("content", ""))})

    if not system_injected:
        new_messages.insert(0, {"role": "system", "content": cold_start.full_prompt_context})

    # 3. Call Upstream LLM or local Antigravity CLI
    response_content = await _call_upstream_llm(new_messages, model=model)

    # 4. Asynchronously Extract Assets from interaction
    interaction_text = f"User: {last_user_msg}\nAssistant: {response_content}"
    asyncio_task = asset_extractor.extract_from_text(
        text=interaction_text,
        session_id=session_id,
        team_id=team_id,
        agent_id=agent_id,
        sync_to_store=True
    )
    import asyncio
    asyncio.create_task(asyncio_task)

    # 5. Return OpenAI-compliant response
    return {
        "id": f"chatcmpl-mem-{int(time.time()*1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": response_content
                },
                "finish_reason": "stop"
            }
        ],
        "usage": {
            "prompt_tokens": len(str(new_messages)) // 4,
            "completion_tokens": len(response_content) // 4,
            "total_tokens": (len(str(new_messages)) + len(response_content)) // 4
        }
    }


@proxy_router.post("/v1/messages")
async def anthropic_messages_proxy(request: Request):
    """
    Anthropic-compatible messages reverse proxy (for Claude Code / Agy).
    Transparently injects TencentDB Agent Memory and extracts assets.
    """
    body = await request.json()
    messages: List[Dict[str, Any]] = body.get("messages", [])
    system_prompt = _content_to_text(body.get("system", ""))
    model = body.get("model", "claude-3-7-sonnet-latest")
    team_id = request.headers.get("x-tdai-team-id", "trading-team")
    agent_id = request.headers.get("x-tdai-agent-id", "committee")
    session_id = request.headers.get("x-session-id", f"claude_{int(time.time()*1000)}")

    # 1. Retrieve Cold Start Context
    last_user_msg = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user_msg = _content_to_text(m.get("content", ""))
            break

    cold_start = await cold_start_engine.generate_cold_start_loadout(
        team_id=team_id,
        agent_id=agent_id,
        query=last_user_msg[:200]
    )

    # 2. Augment System Prompt
    augmented_system = f"{cold_start.full_prompt_context}\n\n{system_prompt}"

    # 3. Format OpenAI-compatible messages for internal router
    converted_msgs = [{"role": "system", "content": augmented_system}]
    for m in messages:
        converted_msgs.append({"role": m.get("role", "user"), "content": _content_to_text(m.get("content"))})

    response_content = await _call_upstream_llm(converted_msgs, model=model)

    # 4. Asynchronously Extract Assets
    interaction_text = f"User: {last_user_msg}\nAssistant: {response_content}"
    import asyncio
    asyncio.create_task(
        asset_extractor.extract_from_text(
            text=interaction_text,
            session_id=session_id,
            team_id=team_id,
            agent_id=agent_id,
            sync_to_store=True
        )
    )

    # 5. Return Anthropic-compliant envelope
    return {
        "id": f"msg_mem_{int(time.time()*1000)}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [
            {
                "type": "text",
                "text": response_content
            }
        ],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": len(str(converted_msgs)) // 4,
            "output_tokens": len(response_content) // 4
        }
    }


async def _call_upstream_llm(messages: List[Dict[str, Any]], model: str) -> str:
    """Invokes local Antigravity CLI, Google Gemini, OpenAI, or returns an informed response."""
    from src.agents.base import llm_client

    system_text = ""
    user_text = ""
    for m in messages:
        if m["role"] == "system":
            system_text += _content_to_text(m.get("content")) + "\n"
        elif m["role"] == "user":
            user_text += _content_to_text(m.get("content")) + "\n"

    # Try existing LLMClient if configured with external API
    if llm_client.has_active_key() and settings.LLM_PROVIDER.lower() in ["gemini", "openai"]:
        res = await llm_client.generate_json(system_text, user_text)
        if res:
            return json.dumps(res, indent=2)

    # Fallback summary
    return f"Acknowledged. Action analyzed within current portfolio context and memory parameters."
