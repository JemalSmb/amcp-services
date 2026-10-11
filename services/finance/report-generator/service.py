"""
report-generator (finance, active) — NOT YET IMPLEMENTED

Automated regulatory report generation.

See README.md in this directory for what's needed to turn this into a
real service, following the pattern in
services/cybersecurity/threat-detection/service.py.
"""

import os

from fastapi import FastAPI

app = FastAPI(title="AMCP report-generator (stub)")


@app.get("/health")
async def health():
    return {
        "service": "report-generator",
        "domain": "finance",
        "status": "not_implemented",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8105)))
