"""Product duration policy, independent of a render workflow's clip capability."""

MIN_EPISODE_SECONDS = 1
MAX_EPISODE_SECONDS = 600
MIN_SHOT_SECONDS = 1
MAX_SHOT_SECONDS = 30
MAX_EPISODE_SHOTS = MAX_EPISODE_SECONDS // MIN_SHOT_SECONDS
PLAN_BATCH_SHOTS = 12

DURATION_POLICY = {
    "min": MIN_EPISODE_SECONDS,
    "max": MAX_EPISODE_SECONDS,
    "presets": [5, 10, 15, 30, 60, 90],
}
