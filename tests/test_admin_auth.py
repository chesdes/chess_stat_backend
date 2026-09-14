import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError

from routers.admin import admin_login, require_admin
from models import AdminLoginRequest
from services.admin_service import AdminService
from starlette.requests import Request

TEST_PASSWORD = "correct-horse"
TEST_SECRET = "test-secret"


class AdminAuthTests(unittest.TestCase):
    def setUp(self):
        patches = [
            patch("services.admin_service.ADMIN_PASSWORD", TEST_PASSWORD),
            patch("services.admin_service.ADMIN_SECRET", TEST_SECRET),
            patch("services.admin_service.ADMIN_TOKEN_TTL_SECONDS", 3600),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _request(self) -> Request:
        return Request({"type": "http", "headers": [], "client": ("9.9.9.9", 12345)})

    def test_login_returns_token_for_correct_password(self):
        response = asyncio.run(admin_login(AdminLoginRequest(password=TEST_PASSWORD), self._request()))
        self.assertEqual(response.expires_in, 3600)
        AdminService().verify_token(response.token)

    def test_login_rejects_wrong_password(self):
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(admin_login(AdminLoginRequest(password="wrong"), self._request()))
        self.assertEqual(ctx.exception.status_code, 401)

    def test_login_rejects_empty_password(self):
        with self.assertRaises(ValidationError):
            AdminLoginRequest(password="")

    def test_token_roundtrip(self):
        token = AdminService().create_token()
        AdminService().verify_token(token)

    def test_expired_token_is_rejected(self):
        expired = jwt.encode(
            {"sub": "admin", "exp": datetime.now(timezone.utc) - timedelta(seconds=1)},
            TEST_SECRET,
            algorithm="HS256",
        )
        with self.assertRaises(ValueError):
            AdminService().verify_token(expired)

    def test_token_signed_with_other_key_is_rejected(self):
        forged = jwt.encode({"sub": "admin"}, "other-key", algorithm="HS256")
        with self.assertRaises(ValueError):
            AdminService().verify_token(forged)

    def test_require_admin_accepts_valid_token(self):
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=AdminService().create_token())
        self.assertIsNone(asyncio.run(require_admin(credentials)))

    def test_require_admin_rejects_missing_or_bad_credentials(self):
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(require_admin(None))
        self.assertEqual(ctx.exception.status_code, 401)

        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(require_admin(HTTPAuthorizationCredentials(scheme="Bearer", credentials="garbage")))
        self.assertEqual(ctx.exception.status_code, 401)


class AdminBruteForceTests(unittest.TestCase):
    """5 failed logins ban the IP for 30 min; the message never reveals the ban."""

    def _store(self):
        return {}

    def _fake_redis(self, store):
        from services import admin_service as mod

        class FakeRedis:
            async def exists(self, key):
                return 1 if key in store else 0

            async def incr(self, key):
                store[key] = store.get(key, 0) + 1
                return store[key]

            async def expire(self, key, ttl):
                return True

            async def set(self, key, value, ex=None):
                store[key] = value
                return True

            async def delete(self, key):
                store.pop(key, None)
                return True

        return FakeRedis()

    def test_five_failures_trigger_ban_with_generic_message(self):
        from services.admin_service import (
            AdminBannedError,
            LOGIN_FAILED_MESSAGE,
            check_login_allowed,
            register_login_failure,
            register_login_success,
        )
        from utils import RedisClient

        store = {}
        with patch.object(RedisClient, "get_client", return_value=self._fake_redis(store)):
            asyncio.run(check_login_allowed("1.2.3.4"))  # not banned yet
            for _ in range(5):
                asyncio.run(register_login_failure("1.2.3.4"))
            with self.assertRaises(AdminBannedError):
                asyncio.run(check_login_allowed("1.2.3.4"))
            self.assertEqual(LOGIN_FAILED_MESSAGE, "Login failed, try again later")

    def test_fail_open_without_redis(self):
        from services.admin_service import check_login_allowed
        from utils import RedisClient

        with patch.object(RedisClient, "get_client", side_effect=RuntimeError("down")):
            self.assertIsNone(asyncio.run(check_login_allowed("1.2.3.4")))


if __name__ == "__main__":
    unittest.main()
