"""
inventory-manager (manufacturing, active) — NOT YET IMPLEMENTED

Smart inventory replenishment and allocation.

See README.md in this directory for what's needed to turn this into a
real service, following the pattern in
services/cybersecurity/threat-detection/service.py.
"""

import os

from fastapi import FastAPI

app = FastAPI(title="AMCP inventory-manager (stub)")


@app.get("/health")
async def health():
    return {
        "service": "inventory-manager",
        "domain": "manufacturing",
        "status": "not_implemented",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8104)))
