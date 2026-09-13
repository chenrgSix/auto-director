import pytest

from app.media.service import media_duration


def assert_full_duration(app, episode):
    """Compare output against source media, independently of script estimates."""
    durations = []
    for shot in episode["shots"]:
        if shot["enabled"]:
            source = app.state.store.get("asset", shot["video_asset_id"])
            actual = media_duration(source["metadata"])
            assert shot["actual_duration"] == pytest.approx(actual, abs=0.002)
            durations.append(actual)
    assert episode["composition_policy"] == "full_clips"
    # Changing FPS can round each complete clip up by less than one frame.
    tolerance = max(0.1, len(durations) / episode["budget"]["fps"])
    assert episode["final_duration"] == pytest.approx(sum(durations), abs=tolerance)
