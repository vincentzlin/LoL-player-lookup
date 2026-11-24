"""Entry point — run with: python run.py

HOST / PORT / RELOAD env vars override the local-dev defaults (used on EC2).
"""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "backend.main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        reload=os.environ.get("RELOAD", "1") == "1",
        log_level="info",
    )
