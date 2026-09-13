from app.agents.audio import PERFORMANCE, VISUAL
from app.agents.directing import anchored_prompt
from app.generation.parameters import resolve_parameters
from tests.test_shot_continuity import contract


def test_unoccupied_video_remains_one_take_without_rewriting_native_audio():
    tail = f"{PERFORMANCE} No speech.\n\noverall_soundscape: Rain.\n\nnon_diegetic_music: None."
    original = f"{VISUAL} [Shot 1] A stationary bowl.\n\n{tail}"
    result = anchored_prompt(
        {}, original, {}, stage="video", visual_continuity=contract(None, framing="insert")
    )
    assert result.startswith(VISUAL)
    assert result.endswith(tail)
    assert "ONE SINGLE CONTINUOUS SHOT" in result
    assert "no person, face, hand" in result
    assert result.index("ONE SINGLE") < result.index(PERFORMANCE)


def test_continuous_camera_motion_and_requested_departures_are_not_locked():
    result = anchored_prompt(
        {},
        "Camera follows the officer out of the room.",
        {},
        stage="video",
        visual_continuity=contract(),
    )
    assert "Camera follows the officer out of the room." in result
    assert "Follow the requested camera motion and action continuously" in result
    assert "no person, face, hand" not in result
    assert "locked" not in result.lower()
    image = anchored_prompt({}, "Reference portrait", {}, stage="image")
    assert "CONTINUOUS SHOT" not in image


def test_explicit_user_video_prompt_still_has_final_authority(system):
    p = system[1].state.store.get("workflow", "default_video")
    binding = p["bindings"]["prompt"]
    key = binding["node_id"] + "." + binding["input"]
    values, _, _, _ = resolve_parameters(
        p,
        {"prompt": "Automatic single take", "duration": 2},
        {},
        {key: "Exact user direction"},
        True,
    )
    assert values["prompt"] == "Exact user direction"
