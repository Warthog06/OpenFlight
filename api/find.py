"""Vercel serverless function for /api/find (the logic lives in openflight.py)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openflight  # noqa: E402

handler = openflight.make_handler(openflight.serverless_app(), fixed_route="/api/find")
