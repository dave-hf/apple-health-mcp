"""HTTPS endpoint that receives Apple Health CSV exports.

The iPhone Health Auto Export app POSTs each export to /ingest as a multipart
body. We strip the MIME envelope and write the inner CSV to disk.
"""
from fastapi import FastAPI, Header, Request, HTTPException
from dotenv import load_dotenv
import os, pathlib
from datetime import datetime

load_dotenv("/opt/health/.env")

TOKEN = os.getenv("HEALTH_TOKEN")
DATA_DIR = pathlib.Path(os.getenv("DATA_DIR", "/opt/health/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI()


@app.get("/health")
def health_check():
    return {"status": "ok"}


def extract_csv(body: bytes) -> bytes:
    """Strip a multipart/form-data envelope when present.

    Health Auto Export sends data as multipart even though only one part is
    used. The CSV sits between the first blank line and the closing boundary
    (`--Boundary-...--`).
    """
    if body.startswith(b"--Boundary"):
        parts = body.split(b"\r\n\r\n", 1)
        if len(parts) == 2:
            csv_and_tail = parts[1]
            lines = csv_and_tail.rsplit(b"\r\n--", 1)
            return lines[0].strip()
    return body


@app.post("/ingest")
async def ingest(request: Request, authorization: str = Header(...)):
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.body()
    csv_data = extract_csv(body)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = DATA_DIR / f"health_{timestamp}.csv"
    dest.write_bytes(csv_data)

    return {"status": "saved", "file": dest.name, "bytes": len(csv_data)}
