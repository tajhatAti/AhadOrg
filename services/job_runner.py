import os
import re
import json
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import logging
from collections import deque
from pathlib import Path
from typing import Optional, List

logger = logging.getLogger("job-runner")

MAX_TIME_MS = int(os.getenv("MAX_EXECUTION_TIME_MS", "10000"))
MAX_MEM_MB = int(os.getenv("MAX_MEMORY_MB", "256"))
EXEC_PIP_TIMEOUT_S = int(os.getenv("EXEC_PIP_TIMEOUT_S", "120"))
JOB_PIP_TIMEOUT_S = int(os.getenv("JOB_PIP_TIMEOUT_S", "240"))
MAX_BG_JOBS = int(os.getenv("MAX_BG_JOBS", "5"))
JOB_LOG_LINES = 2000
JOB_RESTART_LIMIT = 3
JOB_RESTART_DELAY_S = 5

LIVE_PORT_MIN = int(os.getenv("LIVE_PORT_MIN", "11000"))
LIVE_PORT_MAX = int(os.getenv("LIVE_PORT_MAX", "11099"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")

LANGS = {
    "python":     {"ext": "py",   "compile": None,                                                              "run": ["python3", "{file}"]},
    "python3":    {"ext": "py",   "compile": None,                                                              "run": ["python3", "{file}"]},
    "javascript": {"ext": "js",   "compile": None,                                                              "run": ["node", "{file}"]},
    "js":         {"ext": "js",   "compile": None,                                                              "run": ["node", "{file}"]},
    "typescript": {"ext": "ts",   "compile": None,                                                              "run": ["npx", "ts-node", "{file}"]},
    "bash":       {"ext": "sh",   "compile": None,                                                              "run": ["bash", "{file}"]},
    "sh":         {"ext": "sh",   "compile": None,                                                              "run": ["bash", "{file}"]},
    "ruby":       {"ext": "rb",   "compile": None,                                                              "run": ["ruby", "{file}"]},
    "php":        {"ext": "php",  "compile": None,                                                              "run": ["php", "{file}"]},
    "perl":       {"ext": "pl",   "compile": None,                                                              "run": ["perl", "{file}"]},
    "lua":        {"ext": "lua",  "compile": None,                                                              "run": ["lua", "{file}"]},
    "c":          {"ext": "c",    "compile": ["gcc", "{file}", "-o", "{bin}", "-lm", "-std=c11"],                "run": ["{bin}"]},
    "cpp":        {"ext": "cpp",  "compile": ["g++", "{file}", "-o", "{bin}", "-lm", "-std=c++17"],              "run": ["{bin}"]},
    "c++":        {"ext": "cpp",  "compile": ["g++", "{file}", "-o", "{bin}", "-lm", "-std=c++17"],              "run": ["{bin}"]},
    "java":       {"ext": "java", "compile": ["javac", "{file}"],                                                "run": ["java", "-cp", "{dir}", "Main"]},
    "go":         {"ext": "go",   "compile": None,                                                              "run": ["go", "run", "{file}"]},
    "rust":       {"ext": "rs",   "compile": ["rustc", "{file}", "-o", "{bin}"],                                "run": ["{bin}"]},
    "sql":        {"ext": "sql",  "compile": None,                                                              "run": ["sqlite3", ":memory:", ".read {file}"]},
    "text":       {"ext": "txt",  "compile": None,                                                              "run": ["cat", "{file}"]},
}

_jobs: dict = {}
_jobs_lock = threading.Lock()

_STDLIB = set(sys.stdlib_module_names) | {"__future__"}
_IMPORT_TO_PYPI = {
    "pil": "pillow", "cv2": "opencv-python", "imageio": "imageio", "pytesseract": "pytesseract",
    "telegram": "python-telegram-bot", "discord": "discord.py", "pyrogram": "pyrogram",
    "telethon": "telethon", "slack_sdk": "slack-sdk", "twilio": "twilio",
    "bs4": "beautifulsoup4", "lxml": "lxml", "html5lib": "html5lib", "selenium": "selenium",
    "playwright": "playwright", "aiohttp": "aiohttp", "httpx": "httpx",
    "websocket": "websocket-client", "websockets": "websockets", "feedparser": "feedparser",
    "tweepy": "tweepy", "praw": "praw", "sklearn": "scikit-learn", "seaborn": "seaborn",
    "openpyxl": "openpyxl", "xlrd": "xlrd", "yaml": "pyyaml", "dotenv": "python-dotenv",
    "qrcode": "qrcode[pil]", "dateutil": "python-dateutil", "pytz": "pytz", "jinja2": "jinja2",
    "schedule": "schedule", "psutil": "psutil", "watchdog": "watchdog", "crypto": "pycryptodome",
    "jwt": "pyjwt", "multipart": "python-multipart", "pyfiglet": "pyfiglet", "emoji": "emoji",
    "wordcloud": "wordcloud", "pymongo": "pymongo", "psycopg2": "psycopg2-binary",
    "openai": "openai", "anthropic": "anthropic", "groq": "groq", "cohere": "cohere",
    "binance": "python-binance", "pywhatkit": "pywhatkit", "pytubefix": "pytubefix",
    "pytube": "pytube", "moviepy": "moviepy", "pydub": "pydub", "gtts": "gtts",
}

def _set_limits():
    try:
        import resource
        mem_bytes = MAX_MEM_MB * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
    except Exception:
        pass

def _run_subprocess(cmd, cwd, stdin_data, timeout_s, env=None):
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, input=stdin_data, capture_output=True, text=True,
            timeout=timeout_s, preexec_fn=_set_limits if os.name != "nt" else None,
            start_new_session=True, env=env,
        )
        return proc.stdout, proc.stderr, proc.returncode, False
    except subprocess.TimeoutExpired:
        return "", f"Execution timed out after {timeout_s} seconds.", -1, True
    except Exception as e:
        return "", str(e), -1, False

def _detect_imports(code: str) -> list:
    pkgs = set()
    for m in re.finditer(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", code or "", re.MULTILINE):
        mod = m.group(1).lower()
        if mod in _STDLIB: continue
        pkgs.add(_IMPORT_TO_PYPI.get(mod, mod))
    for line in (code or "").splitlines()[:40]:
        m = re.match(r"^\s*#\s*requirements\s*[::]\s*(.+)$", line, re.IGNORECASE)
        if m: pkgs.update(p for p in re.split(r"[,\s]+", m.group(1).strip()) if p)
    return sorted(pkgs)[:20]

def _alloc_port() -> Optional[int]:
    with _jobs_lock:
        used = {j.get("port") for j in _jobs.values() if j.get("port")}
    for p in range(LIVE_PORT_MIN, LIVE_PORT_MAX + 1):
        if p not in used: return p
    return None

def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return (s[:18].strip("-") or "job") + "-" + secrets.token_hex(3)

def _spawn(j: dict) -> None:
    cfg = LANGS[j["lang"]]
    cmd = [c.replace("{file}", j["file"]).replace("{bin}", j["bin"]).replace("{dir}", j["dir"]) for c in cfg["run"]]
    env = dict(os.environ)
    if j.get("pylibs"):
        env["PYTHONPATH"] = j["pylibs"] + os.pathsep + env.get("PYTHONPATH", "")
    
    # Inject user environment variables
    if j.get("env_vars"):
        try:
            user_envs = json.loads(j["env_vars"])
            if isinstance(user_envs, dict):
                for k, v in user_envs.items():
                    env[str(k)] = str(v)
        except Exception:
            pass

    if j.get("port"):
        env["PORT"] = str(j["port"])
        env["HOST"] = "0.0.0.0"
    
    proc = subprocess.Popen(
        cmd, cwd=j["dir"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, preexec_fn=_set_limits if os.name != "nt" else None,
        start_new_session=True, env=env,
    )
    j["proc"] = proc
    j["status"] = "running"
    j["log"].append(f"[system] started (pid {proc.pid})")
    j["web"] = False

    if j.get("port"):
        from services.proxy import _web_watch
        threading.Thread(target=_web_watch, args=(j, proc, j["port"]), daemon=True).start()

    def _reader():
        try:
            for line in proc.stdout:
                j["log"].append(line.rstrip("\n"))
        except Exception: pass

    def _supervisor():
        rc = proc.wait()
        j["log"].append(f"[system] exited with code {rc}")
        if j.get("stop_requested"):
            j["status"] = "stopped"
            return
        if j["restart_enabled"] and j["restarts"] < JOB_RESTART_LIMIT:
            j["restarts"] += 1
            j["log"].append(f"[system] restarting in {JOB_RESTART_DELAY_S}s (attempt {j['restarts']}/{JOB_RESTART_LIMIT})")
            time.sleep(JOB_RESTART_DELAY_S)
            if not j.get("stop_requested"): _spawn(j)
        else:
            j["status"] = "crashed" if rc != 0 else "stopped"

    threading.Thread(target=_reader, daemon=True).start()
    threading.Thread(target=_supervisor, daemon=True).start()

def _prepare_and_run(j: dict, reqs: list) -> None:
    if reqs:
        j["log"].append(f"[system] Installing libraries: {', '.join(reqs)}")
        deadline = time.monotonic() + JOB_PIP_TIMEOUT_S
        for spec in reqs:
            remain = int(deadline - time.monotonic())
            if remain <= 0: break
            name = spec.split("[")[0].split("=")[0].split("<")[0].split(">")[0].strip()
            _, _, tcode, _ = _run_subprocess(
                ["python3", "-m", "pip", "install", "--quiet", "--target", j["pylibs"], spec],
                j["dir"], None, remain,
            )
            if tcode == 0: j["log"].append(f"[system] ✓ {name} installed")
            else: j["log"].append(f"[system] ✗ {name} failed")
    _spawn(j)

def start_job(language: str, code: str, name: str = "", restart: bool = True, env_vars: str = None) -> dict:
    lang = language.lower().strip()
    if lang not in LANGS: raise ValueError(f"Unsupported language: {lang}")
    
    job_id = uuid.uuid4().hex[:12]
    jdir = tempfile.mkdtemp(prefix=f"job_{job_id}_")
    cfg = LANGS[lang]
    src = os.path.join(jdir, "main." + cfg["ext"])
    binf = os.path.join(jdir, "main.bin")
    with open(src, "w") as f: f.write(code[:262144])

    if cfg["compile"]:
        ccmd = [c.replace("{file}", src).replace("{bin}", binf).replace("{dir}", jdir) for c in cfg["compile"]]
        _, cerr, ccode, _ = _run_subprocess(ccmd, jdir, None, MAX_TIME_MS / 1000)
        if ccode != 0:
            shutil.rmtree(jdir, ignore_errors=True)
            raise ValueError(f"Compilation failed: {cerr}")

    reqs = _detect_imports(code)
    pylibs = os.path.join(jdir, "pylibs") if reqs else None
    if pylibs: os.makedirs(pylibs, exist_ok=True)

    job = {
        "id": job_id, "name": name[:60] or "job", "lang": lang, "dir": jdir,
        "file": src, "bin": binf, "pylibs": pylibs, "proc": None,
        "status": "installing" if reqs else "starting",
        "log": deque(maxlen=JOB_LOG_LINES), "restarts": 0, "restart_enabled": restart,
        "stop_requested": False, "started_at": time.time(), "port": _alloc_port(),
        "web": False, "web_slug": _slugify(name or job_id), "web_public": True,
        "access_key": secrets.token_urlsafe(12),
        "env_vars": env_vars,
    }
    with _jobs_lock: _jobs[job_id] = job
    if reqs: threading.Thread(target=_prepare_and_run, args=(job, reqs), daemon=True).start()
    else: _spawn(job)
    return job

def stop_job(job_id: str) -> bool:
    j = _jobs.get(job_id)
    if not j: return False
    j["stop_requested"] = True
    proc = j.get("proc")
    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=3)
        except Exception:
            try: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception: pass
    j["status"] = "stopped"
    j["web"] = False
    with _jobs_lock: j["port"] = None
    if os.path.isdir(j["dir"]): shutil.rmtree(j["dir"], ignore_errors=True)
    return True

def get_job_info(job_id: str) -> Optional[dict]:
    return _jobs.get(job_id)

def list_all_jobs() -> List[dict]:
    with _jobs_lock: return list(_jobs.values())
