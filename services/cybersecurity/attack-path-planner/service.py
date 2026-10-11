"""
attack-path-planner (cybersecurity, active) — NOT YET IMPLEMENTED

Attack path prediction and blocking.

See README.md in this directory for what's needed to turn this into a
real service, following the pattern in
services/cybersecurity/threat-detection/service.py.
"""

import os

from fastapi import FastAPI

app = FastAPI(title="AMCP attack-path-planner (stub)")


@app.get("/health")
async def health():
    return {
        "service": "attack-path-planner",
        "domain": "cybersecurity",
        "status": "not_implemented",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8103)))
