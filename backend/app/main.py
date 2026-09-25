from fastapi import FastAPI

app = FastAPI(title="Agentic SRE Trust Lab", version="0.1.1")


@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    return {"service": "backend", "status": "ok"}
