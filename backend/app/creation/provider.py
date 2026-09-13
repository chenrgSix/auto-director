from app.core.errors import AppError


class ExternalCreationProvider:
    """Packages own text. Only an explicitly selected visual reviewer may call a model."""

    def __init__(self, factory, *, visual_review):
        self.factory, self.visual_review = factory, visual_review
        self.provider = None
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    async def generate_json(self, *args, images=None, **kwargs):
        if not images or self.visual_review != "model":
            raise AppError(
                "CREATION_PACKAGE_REQUIRED",
                "请在创作项目中补齐或修改创作包，再提交新版本；此制作不调用文字模型",
                status=409,
            )
        if self.provider is None:
            self.provider = self.factory()
            self.usage = self.provider.usage
        return await self.provider.generate_json(*args, images=images, **kwargs)
