from __future__ import annotations

from fastapi import APIRouter
from titiler.core.factory import TilerFactory


def create_cog_router() -> APIRouter:
    cog = TilerFactory()
    return cog.router
