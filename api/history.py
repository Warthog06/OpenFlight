"""Vercel serverless function for /api/history (the logic lives in openflight.py)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openflight  # noqa: E402

# Vercel only treats this file as a function if it declares "class handler" directly.
_Base = openflight.make_handler(openflight.serverless_app(), fixed_route="/api/history")


class handler(_Base):
    pass
