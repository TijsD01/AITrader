import asyncio
from unittest.mock import patch

from src.agents.base import LLMClient


class _Proc:
    def __init__(self, rc, out=b""):
        self.returncode, self._out = rc, out

    async def communicate(self):
        return self._out, b"err"


def _run(rcs):
    procs = [_Proc(rc, b'{"ok": true}' if rc == 0 else b"") for rc in rcs]
    calls = []

    async def fake_exec(*a, **k):
        calls.append(a)
        return procs[len(calls) - 1]

    async def no_sleep(_):
        pass

    client = LLMClient()
    with patch.object(LLMClient, "_resolve_agy_path", return_value="/x/agy"), \
         patch("asyncio.create_subprocess_exec", fake_exec), \
         patch("asyncio.sleep", no_sleep):
        res = asyncio.run(client._call_antigravity("s", "u"))
    return res, len(calls)


def test_signal_death_retried():
    assert _run([-11, 0]) == ({"ok": True}, 2)


def test_positive_exit_not_retried():
    assert _run([1]) == (None, 1)
