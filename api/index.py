"""Vercel serverless entrypoint for the GridWise FastAPI app.

Exposes the FastAPI ``app`` instance as ``handler`` so Vercel's Python
runtime can dispatch every request to it. All routes (including ``/``,
``/health``, and ``/optimize-energy``) are served from the same app.
"""

from app.main import app  # noqa: F401  (re-exported for Vercel)

# Vercel's @vercel/python runtime looks for a top-level ``handler`` symbol
# that accepts (request, response) for the legacy wsgi-style handler. We
# use ASGI via the ``asgi`` symbol instead, which is the supported path
# for FastAPI / Starlette apps on Vercel.
handler = app
asgi = app
