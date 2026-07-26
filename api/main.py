import os

from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()

app = FastAPI(title="Infra Portal API")


def is_mock_mode() -> bool:
    return os.getenv("USE_MOCK", "true").strip().lower() == "true"


@app.get("/health")
def health() -> dict:
    return {"ok": True, "mock": is_mock_mode()}
