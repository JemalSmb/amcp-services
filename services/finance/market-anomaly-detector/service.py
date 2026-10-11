"""
market-anomaly-detector (finance, passive) — NOT YET IMPLEMENTED

Detect unusual market patterns and manipulation.

See README.md in this directory for what's needed to turn this into a
real service, following the pattern in
services/cybersecurity/threat-detection/service.py.
"""

import os

from fastapi import FastAPI

app = FastAPI(title="AMCP market-anomaly-detector (stub)")


@app.get("/health")
async def health():
    return {
        "service": "market-anomaly-detector",
        "domain": "finance",
        "status": "not_implemented",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8004)))
