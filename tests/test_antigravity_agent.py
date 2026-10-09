import pytest
from src.agents.base import llm_client

@pytest.mark.asyncio
async def test_antigravity_llm_client():
    assert llm_client.has_active_key() is True
    assert llm_client.provider == "antigravity"

    res = await llm_client.generate_json(
        system_prompt="You are a financial quantitative analysis assistant.",
        user_prompt='Analyze market conditions. Return a JSON object with: {"status": "success", "market_bias": "BULLISH", "confidence": 85}'
    )
    assert res is not None
    assert isinstance(res, dict)
    assert "status" in res or "market_bias" in res or "confidence" in res
