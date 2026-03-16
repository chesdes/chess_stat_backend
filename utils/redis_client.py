import redis.asyncio as redis
from typing import Optional

class RedisClient:
    _client: Optional[redis.Redis] = None

    @classmethod
    def init(cls, host="redis", port=6379, db=0):
        cls._client = redis.Redis(host=host, port=port, db=db)

    @classmethod
    def get_client(cls) -> redis.Redis:
        if cls._client is None:
            raise RuntimeError("Redis not initialized. Call RedisClient.init() first.")
        return cls._client
