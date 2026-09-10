import httpx
import pytest

from app.comfyui.client import ComfyUIClient
from app.core.config import Settings
from app.core.errors import AppError


async def noop(_):
    pass


async def test_submission_network_failure_is_unknown_and_never_retried():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        raise httpx.ReadTimeout("lost response", request=request)

    async with ComfyUIClient(
        Settings(_env_file=None),
        "http://127.0.0.1:8188",
        transport=httpx.MockTransport(handler),
        use_websocket=False,
    ) as client:
        with pytest.raises(AppError, match="响应丢失") as error:
            await client.execute({}, "job", noop, noop, lambda: False)
        assert error.value.code == "SUBMISSION_UNKNOWN"
    assert calls == ["/prompt"]


async def test_prompt_history_association_and_output_filtering():
    submitted = []

    def handler(request):
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "ours"})
        assert request.url.path == "/history/ours"
        return httpx.Response(
            200,
            json={
                "ours": {
                    "status": {"completed": True, "status_str": "success"},
                    "outputs": {
                        "selected": {"images": [{"filename": "clip.mp4", "type": "output"}]},
                        "other": {"images": [{"filename": "wrong.png"}]},
                    },
                }
            },
        )

    async def save(data):
        submitted.append(data)

    async with ComfyUIClient(
        Settings(_env_file=None),
        "http://127.0.0.1:8188",
        transport=httpx.MockTransport(handler),
        use_websocket=False,
    ) as client:
        history = await client.execute({}, "job", save, noop, lambda: False)
        assert client.outputs(history, "selected", "video")[0]["filename"] == "clip.mp4"
        assert client.outputs(history, "selected", "image") == []
    assert submitted == [{"comfy_prompt_id": "ours"}]


async def test_cancel_does_not_interrupt_somebody_elses_prompt():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(
            200, json={"queue_running": [[0, "someone-else"]], "queue_pending": [[1, "ours"]]}
        )

    async with ComfyUIClient(
        Settings(_env_file=None), "http://127.0.0.1:8188", transport=httpx.MockTransport(handler)
    ) as client:
        await client.cancel("ours")
    assert paths == ["/queue", "/queue"]


async def test_download_rejects_path_traversal(tmp_path):
    async with ComfyUIClient(
        Settings(_env_file=None),
        "http://127.0.0.1:8188",
        transport=httpx.MockTransport(lambda _: httpx.Response(200)),
    ) as client:
        with pytest.raises(AppError):
            await client.download({"filename": "../../secrets"}, tmp_path / "asset")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={}),
        httpx.Response(503, json={"error": "upstream"}),
    ],
)
async def test_corrupted_submit_response_remains_unknown(response):
    async with ComfyUIClient(
        Settings(_env_file=None),
        "http://127.0.0.1:8188",
        transport=httpx.MockTransport(lambda _: response),
        use_websocket=False,
    ) as client:
        with pytest.raises(AppError) as result:
            await client.execute({}, "job", noop, noop, lambda: False)
        assert result.value.code == "SUBMISSION_UNKNOWN"
