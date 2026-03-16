import aiohttp
import asyncio

async def get_json(url: str, headers: dict | None = None, timeout: int = 10):
    headers = headers or {}
    headers["User-Agent"] = "chess-stats-api/1.0"

    try:
        timeout_obj = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=timeout_obj, headers=headers) as session:
            async with session.get(url) as response:
                response.raise_for_status()
                return await response.json()
    except aiohttp.ClientError as e:
        print(f"[HTTP ERROR] {url}: {e}")
        return None
    except asyncio.TimeoutError:
        print(f"[TIMEOUT] {url}")
        return None
