"""Unit tests for fail-closed auth when API_KEY is empty in production (DAAI-158)."""

import pytest
from unittest.mock import patch
from fastapi import HTTPException


class TestFailClosedAuth:
    @pytest.mark.asyncio
    async def test_empty_api_key_local_allows_access(self):
        """In local/dev, empty API_KEY still allows access (backward compat)."""
        with patch("app.core.auth.API_KEY", ""), patch("app.core.auth.IS_LOCAL", True):
            from app.core.auth import verify_api_key

            result = await verify_api_key(api_key=None)
            assert result == ""

    @pytest.mark.asyncio
    async def test_empty_api_key_production_returns_503(self):
        """In production, empty API_KEY raises 503 (fail-closed)."""
        with patch("app.core.auth.API_KEY", ""), patch("app.core.auth.IS_LOCAL", False):
            from app.core.auth import verify_api_key

            with pytest.raises(HTTPException) as exc:
                await verify_api_key(api_key=None)
            assert exc.value.status_code == 503

    @pytest.mark.asyncio
    async def test_empty_api_key_production_global_key_with_org_returns_503(self):
        """verify_global_key_with_org also fails closed in production."""
        with patch("app.core.auth.API_KEY", ""), patch("app.core.auth.IS_LOCAL", False):
            from app.core.auth import verify_global_key_with_org

            with pytest.raises(HTTPException) as exc:
                await verify_global_key_with_org(
                    x_api_key="anything", x_org_id="org-123"
                )
            assert exc.value.status_code == 503

    @pytest.mark.asyncio
    async def test_valid_api_key_production_works(self):
        """Normal auth flow still works when API_KEY is configured."""
        with (
            patch("app.core.auth.API_KEY", "secret"),
            patch("app.core.auth.IS_LOCAL", False),
        ):
            from app.core.auth import verify_api_key

            result = await verify_api_key(api_key="secret")
            assert result == "secret"
