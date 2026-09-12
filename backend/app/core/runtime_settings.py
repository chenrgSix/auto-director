import asyncio
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from app.core.config import Settings
from app.core.errors import AppError
from app.core.security import validate_comfy_url


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    comfyui_url: str | None = Field(default=None, min_length=1, max_length=500)
    allow_public_comfyui: bool | None = None
    llm_base_url: str | None = Field(default=None, min_length=1, max_length=500)
    llm_model: str | None = Field(default=None, max_length=200)
    vlm_model: str | None = Field(default=None, max_length=200)
    llm_timeout: float | None = Field(default=None, ge=1, le=3600)
    prompt_batch_size: int | None = Field(default=None, ge=1, le=3, strict=True)
    llm_api_key: SecretStr | None = Field(default=None, max_length=4096)
    clear_llm_api_key: bool = False
    render_timeout: float | None = Field(default=None, ge=1, le=14400)
    request_timeout: float | None = Field(default=None, ge=1, le=120)
    max_asset_mb: int | None = Field(default=None, ge=1, le=2048)
    poll_interval: float | None = Field(default=None, ge=0.01, le=30)

    @model_validator(mode="after")
    def check_patch(self):
        for name in self.model_fields_set - {"llm_api_key"}:
            if getattr(self, name) is None:
                raise ValueError("配置字段不能为 null")
        if (
            self.clear_llm_api_key
            and self.llm_api_key
            and self.llm_api_key.get_secret_value().strip()
        ):
            raise ValueError("不能同时替换和清除密钥")
        return self


ONLINE_FIELDS = set(SettingsPatch.model_fields) - {"clear_llm_api_key"}


def model_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and not any(char.isspace() for char in value.strip())
            and (port is None or port > 0)
        )
    except ValueError:
        valid = False
    if not valid:
        raise AppError("INVALID_URL", "模型端点必须是无凭据和查询参数的 HTTP(S) 地址", status=422)
    return value.strip().rstrip("/")


class RuntimeSettings:
    """One shared Settings object; file persistence precedes atomic in-memory publication."""

    def __init__(self, settings: Settings, legacy: dict):
        self.settings = settings
        self.path = settings.storage_root / "runtime-settings.json"
        self.lock = asyncio.Lock()
        self.overrides = {}
        if legacy.get("comfyui_url"):
            settings.comfyui_url = legacy["comfyui_url"]
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                if not isinstance(data, dict) or not data.keys() <= ONLINE_FIELDS:
                    raise ValueError("invalid settings")
                # Validate persisted fields without exposing their values in startup errors.
                SettingsPatch.model_validate(data)
                effective = Settings.model_validate({**settings.model_dump(), **data})
                os.chmod(self.path, 0o600)
            except (OSError, ValueError, ValidationError):
                raise AppError(
                    "CONFIGURATION_INVALID",
                    "在线配置文件无法读取或格式无效，请检查 runtime-settings.json",
                ) from None
            self.overrides = data
            settings.__dict__.update(effective.__dict__)

    def public(self) -> dict:
        result = self.settings.model_dump(include=ONLINE_FIELDS - {"llm_api_key"})
        # Legacy environment URLs may embed credentials; never echo those into the editor.
        for field in ("comfyui_url", "llm_base_url"):
            try:
                parsed = urlsplit(result[field])
                result[field] = urlunsplit(
                    (parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", "")
                )
            except ValueError:
                result[field] = ""
        return {
            **result,
            "llm_configured": bool(self.settings.llm_model),
            "vlm_configured": bool(self.settings.vlm_model),
            "llm_api_key_configured": bool(self.settings.llm_api_key),
        }

    def persist(self, values: dict) -> None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix=".settings-", dir=self.path.parent, delete=False
            ) as file:
                temporary = Path(file.name)
                os.fchmod(file.fileno(), 0o600)
                json.dump(values, file, ensure_ascii=False, allow_nan=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        except OSError:
            raise AppError(
                "CONFIGURATION_SAVE_FAILED", "配置未保存，请检查数据目录写入权限", status=500
            ) from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def update(self, body: SettingsPatch, busy) -> dict:
        async with self.lock:
            changes = body.model_dump(
                exclude_unset=True, exclude={"llm_api_key", "clear_llm_api_key"}
            )
            for field in ("llm_model", "vlm_model"):
                if field in changes:
                    changes[field] = changes[field].strip()
            if "llm_base_url" in changes:
                changes["llm_base_url"] = model_url(changes["llm_base_url"])
            key = body.llm_api_key.get_secret_value().strip() if body.llm_api_key else ""
            if key:
                if any(char in key for char in "\r\n"):
                    raise AppError("INVALID_CONFIGURATION", "密钥不能包含换行", status=422)
                changes["llm_api_key"] = key
            elif body.clear_llm_api_key:
                changes["llm_api_key"] = ""
            if (
                "llm_base_url" in changes
                and changes["llm_base_url"] != self.settings.llm_base_url.rstrip("/")
                and self.settings.llm_api_key
                and "llm_api_key" not in changes
            ):
                raise AppError(
                    "CREDENTIAL_REQUIRED",
                    "切换模型端点时请重新填写密钥，或明确选择清除密钥",
                    status=409,
                )
            candidate = Settings.model_validate({**self.settings.model_dump(), **changes})
            if {"comfyui_url", "allow_public_comfyui"} & changes.keys():
                try:
                    changes["comfyui_url"] = await validate_comfy_url(
                        candidate.comfyui_url.strip(), candidate.allow_public_comfyui
                    )
                except AppError as exc:
                    if exc.code == "INVALID_URL":
                        raise AppError(exc.code, exc.message, status=422) from None
                    raise
                candidate.comfyui_url = changes["comfyui_url"]
            # Recheck after DNS awaits: enqueue may have happened while validating the URL.
            if busy():
                raise AppError(
                    "CONFLICT",
                    "存在进行中、尚未退出或状态不确定的作业，请结束并核对后再保存配置",
                    status=409,
                )
            if changes:
                values = {**self.overrides, **changes}
                self.persist(values)
                self.overrides = values
                self.settings.__dict__.update(candidate.__dict__)
            return self.public()
