from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AD_", env_file=ROOT / ".env", extra="ignore")

    data_dir: Path = ROOT / "data"
    comfyui_url: str = "http://127.0.0.1:8188"
    allow_public_comfyui: bool = False
    llm_base_url: str = "http://127.0.0.1:11434/v1"
    llm_model: str = ""
    llm_api_key: str = Field(default="", repr=False)
    vlm_model: str = ""
    render_timeout: float = Field(default=1800, ge=1, le=14400)
    request_timeout: float = Field(default=30, ge=1, le=120)
    max_asset_mb: int = Field(default=512, ge=1, le=2048)
    poll_interval: float = Field(default=1, ge=0.01, le=30)
    allowed_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    @property
    def storage_root(self) -> Path:
        return (ROOT / self.data_dir).resolve()
