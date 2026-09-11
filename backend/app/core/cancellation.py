import asyncio
from collections.abc import Awaitable, Callable


async def run_cancellable[T](
    operation: Callable[[], Awaitable[T]], check_cancel: Callable[[], None]
) -> T:
    """Cancel local/read-only waits, retaining ownership until their cleanup finishes."""
    check_cancel()
    task = asyncio.ensure_future(operation())
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=0.1)
            check_cancel()
        return task.result()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
