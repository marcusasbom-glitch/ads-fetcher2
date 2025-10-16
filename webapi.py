# webapi.py
from fastapi import FastAPI, Form
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/")
def root():
    return {"ok": True, "paths": ["/run (POST)", "/health"]}

@app.get("/health")
def health():
    return {"status": "healthy"}

# Acceptera både /run och /run/ för att undvika 404 pga snedstreck
@app.post("/run")
@app.post("/run/")
async def run(ar_input: str = Form(...), email: str | None = Form(None)):
    # Minimal “eko”-respons för att verifiera flödet
    return {
        "job_id": "test-" + ar_input[:12],
        "status": "queued",
        "echo": {"ar_input": ar_input, "email": email}
    }
