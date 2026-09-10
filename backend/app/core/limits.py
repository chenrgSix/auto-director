"""Product duration policy, independent of a render workflow's clip capability."""

MIN_EPISODE_SECONDS = 1
MAX_EPISODE_SECONDS = 600
MIN_SHOT_SECONDS = 1
MAX_SHOT_SECONDS = 30
MAX_EPISODE_SHOTS = 240
LEGACY_MAX_SHOTS = MAX_EPISODE_SECONDS // MIN_SHOT_SECONDS
PLAN_BATCH_SHOTS = 12

DURATION_POLICY = {
    "min": MIN_EPISODE_SECONDS,
    "max": MAX_EPISODE_SECONDS,
    "presets": [5, 10, 15, 30, 60, 90],
}

MIN_DIMENSION = 256
MAX_DIMENSION = 2048
MAX_FPS = 60
MAX_BATCH = 8
LIMITS = {
    "episode_seconds": {"min": MIN_EPISODE_SECONDS, "max": MAX_EPISODE_SECONDS},
    "episode_shots": MAX_EPISODE_SHOTS,
    "shot_seconds": {"min": MIN_SHOT_SECONDS, "max": MAX_SHOT_SECONDS},
    "dimension": {"min": MIN_DIMENSION, "max": MAX_DIMENSION, "multiple": 16},
    "fps": {"min": 1, "max": MAX_FPS},
    "batch": {"min": 1, "max": MAX_BATCH},
}
