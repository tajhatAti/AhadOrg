import os
import time
import tempfile
import shutil
from fastapi import APIRouter, Header, HTTPException, Request
from typing import Optional

from models import ExecuteCodeRequest
from utils import rate_limit, client_ip
from auth_deps import get_current_user_and_session
from services.job_runner import LANGS, _run_subprocess, _detect_imports

router = APIRouter(tags=["execute"])

MAX_TIME_MS = int(os.getenv("MAX_EXECUTION_TIME_MS", "10000"))
EXEC_PIP_TIMEOUT_S = int(os.getenv("EXEC_PIP_TIMEOUT_S", "120"))

@router.post("/api/execute")
async def execute_code(payload: ExecuteCodeRequest, request: Request, authorization: Optional[str] = Header(None)):
    get_current_user_and_session(authorization)
    rate_limit(f"{client_ip(request)}:exec")

    lang = payload.language.lower().strip()
    if lang not in LANGS: raise HTTPException(400, detail="Unsupported language.")
    
    cfg = LANGS[lang]
    start = time.monotonic()
    tmpdir = tempfile.mkdtemp(prefix="run_")
    try:
        src = os.path.join(tmpdir, "main." + cfg["ext"])
        binf = os.path.join(tmpdir, "main.bin")
        with open(src, "w") as f: f.write(payload.code[:262144])

        reqs = _detect_imports(payload.code)
        run_env = None
        if reqs:
            pylibs = os.path.join(tmpdir, "pylibs")
            os.makedirs(pylibs, exist_ok=True)
            _run_subprocess(["python3", "-m", "pip", "install", "--quiet", "--target", pylibs] + reqs, tmpdir, None, EXEC_PIP_TIMEOUT_S)
            run_env = dict(os.environ)
            run_env["PYTHONPATH"] = pylibs + os.pathsep + run_env.get("PYTHONPATH", "")

        if cfg["compile"]:
            ccmd = [c.replace("{file}", src).replace("{bin}", binf).replace("{dir}", tmpdir) for c in cfg["compile"]]
            _, cerr, ccode, _ = _run_subprocess(ccmd, tmpdir, None, MAX_TIME_MS / 1000, env=run_env)
            if ccode != 0: return {"success": False, "stderr": cerr, "exit_code": ccode}

        rcmd = [c.replace("{file}", src).replace("{bin}", binf).replace("{dir}", tmpdir) for c in cfg["run"]]
        stdout, stderr, exit_code, timed_out = _run_subprocess(rcmd, tmpdir, payload.stdin or "", MAX_TIME_MS / 1000, env=run_env)

        return {
            "success": exit_code == 0 and not timed_out,
            "stdout": stdout[:65536],
            "stderr": stderr[:65536],
            "exit_code": exit_code,
            "execution_time_ms": int((time.monotonic() - start) * 1000),
            "error": "Timed out." if timed_out else None
        }
    finally: shutil.rmtree(tmpdir, ignore_errors=True)
