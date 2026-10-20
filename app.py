"""Vercel FastAPI entrypoint. The application is defined in api/index.py."""

from api.index import app

__all__ = ["app"]
