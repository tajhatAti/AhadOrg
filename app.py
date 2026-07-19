import os
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from database import init_db
from routes import auth, profile, code_editor, runspace, admin, dashboard, execute
from services.proxy import proxy_request, _live_page
from services.job_runner import get_job_info

# Initialize Database
init_db()

app = FastAPI(title="RunSpace")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
INDEX_FILE = BASE_DIR / "index.html"

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Include Routers
app.include_router(auth.router)
app.include_router(profile.router)
app.include_router(code_editor.router)
app.include_router(runspace.router)
app.include_router(admin.router)
app.include_router(dashboard.router)
app.include_router(execute.router)

@app.get("/", include_in_schema=False)
def read_root():
    return FileResponse(INDEX_FILE)

# Client-side routing: serve SPA for these paths
CLIENT_ONLY_PATHS = [
    "dashboard", "code", "jobs", "runspace", "admin", "activity",
    "sign-in", "sign-up", "login", "forgot", "profile"
]
for path in CLIENT_ONLY_PATHS:
    @app.get(f"/{path}", include_in_schema=False)
    def spa_route():
        return FileResponse(INDEX_FILE)

# Live Proxy
@app.api_route("/live/{slug}/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"], include_in_schema=False)
@app.api_route("/live/{slug}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"], include_in_schema=False)
async def live_proxy(slug: str, request: Request, full_path: str = ""):
    from services.job_runner import list_all_jobs
    jobs = list_all_jobs()
    job = next((j for j in jobs if j["web_slug"] == slug), None)
    
    if not job:
        return _live_page("Not Found", "<h1>No job lives at this address</h1>")
    
    if not job["web"]:
         return _live_page("Starting", "<h1>The job is starting, please wait...</h1>")

    return await proxy_request(job, request, full_path)

@app.get("/health")
def health():
    return {"status": "ok"}
