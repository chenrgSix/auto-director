import asyncio
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlsplit

from app.core.errors import AppError


async def validate_comfy_url(url: str, allow_public: bool = False) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise AppError("INVALID_URL", "ComfyUI 地址必须是无凭据、路径、查询参数的 HTTP(S) 地址")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        resolved = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, port)
    except (OSError, ValueError) as exc:
        raise AppError("COMFYUI_OFFLINE", "无法解析 ComfyUI 地址", status=502) from exc
    if not resolved:
        raise AppError("COMFYUI_OFFLINE", "ComfyUI 地址无可用解析", status=502)
    for item in resolved:
        address = ipaddress.ip_address(item[4][0])
        if address.is_link_local or address.is_multicast or address.is_unspecified:
            raise AppError("INVALID_URL", "不允许 metadata、link-local 或 multicast 地址")
        lan = any(
            address in ipaddress.ip_network(network)
            for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
            if address.version == ipaddress.ip_network(network).version
        )
        if not allow_public and not (address.is_loopback or lan):
            raise AppError("INVALID_URL", "公网 ComfyUI 需设置 AD_ALLOW_PUBLIC_COMFYUI=true")
    return url.rstrip("/")


def safe_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise AppError("INVALID_PATH", "文件必须位于本地资产目录")
    return path
