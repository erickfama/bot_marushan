from __future__ import annotations

import pytest

from src.views import PlayerControls


@pytest.mark.asyncio
async def test_stop_button_does_not_override_view_lifecycle_stop() -> None:
    view = PlayerControls(object())  # type: ignore[arg-type]
    assert callable(view.stop)
    assert view.stop_button is not view.stop
    view.stop()
