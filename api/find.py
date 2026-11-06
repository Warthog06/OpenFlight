"""Vercel serverless function for /api/find (the logic lives in server.py)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

handler = server.make_handler(server.serverless_app(), fixed_route="/api/find")
