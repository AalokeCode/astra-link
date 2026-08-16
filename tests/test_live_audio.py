from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.voice.gemini_live import LiveQuotaExceeded, LiveQuotaGuard

@pytest.mark.asyncio
async def test_live_quota_is_shared_and_persists_session_count(tmp_path):
    cfg = SimpleNamespace(
        data_dir=tmp_path,
        live_max_concurrent_sessions=1,
        live_max_session_seconds=600,
        live_max_daily_minutes=60,
    )
    guard = LiveQuotaGuard(cfg)

    async with guard.lease() as allowed:
        assert allowed == 600
        with pytest.raises(LiveQuotaExceeded, match="slots"):
            async with guard.lease():
                pass

    restored = LiveQuotaGuard(cfg)
    snapshot = await restored.snapshot()
    assert snapshot.sessions_started_today == 1
    assert snapshot.active_sessions == 0
