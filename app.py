"""
Source Evaluator Web Interface
FastAPI backend wrapping source_eval_v6.py via subprocess

Supports two modes:
- Synchronous (Vercel serverless): POST /api/evaluate returns results directly
- Async (local/Railway): POST /api/evaluate-async starts a background job
"""
import json, asyncio, uuid, time, subprocess, tempfile, os, sys, shutil
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()  # Load .env file (ANTHROPIC_API_KEY, etc.)

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="HRF Source Evaluator")

PROJECT_DIR = Path(__file__).parent
SCRIPT = PROJECT_DIR / "v6-v10" / "source_eval_v6.py"

# Detect if running on Vercel (serverless) vs local/Railway (server)
IS_VERCEL = os.environ.get("VERCEL", "") == "1"

# On Vercel, only /tmp is writable; locally use project dir
CACHE_DIR = Path("/tmp/.cache_web_eval") if IS_VERCEL else PROJECT_DIR / ".cache_web_eval"

# Find Python: use local venv if available, otherwise system python
_venv_python = PROJECT_DIR / ".venv312" / "bin" / "python3"
PYTHON = str(_venv_python) if _venv_python.exists() else sys.executable

# Max URLs per request on Vercel (must complete within function timeout)
VERCEL_MAX_URLS = 10

# ── In-memory job store (only used for local/Railway async mode) ──
jobs: dict = {}


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = PROJECT_DIR / "templates" / "index.html"
    return HTMLResponse(html_path.read_text())


def _run_evaluation_sync(urls: list, intended_use: str, use_llm: bool) -> dict:
    """
    Run source evaluation synchronously via subprocess.
    Returns dict with 'results' or 'error'.
    """
    tmp = None
    out_json = None
    out_md = None
    try:
        # Write URLs to a temp file
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        for u in urls:
            tmp.write(u + "\n")
        tmp.close()

        # Output files
        out_json = tempfile.mktemp(suffix=".json")
        out_md = tempfile.mktemp(suffix=".md")

        cmd = [
            str(PYTHON), str(SCRIPT),
            "--works-cited", tmp.name,
            "--intended-use", intended_use.upper(),
            "--cache-dir", str(CACHE_DIR),
            "--out-json", out_json,
            "--out-md", out_md,
            "--sleep-s", "0.5",
        ]
        if not use_llm:
            cmd.append("--no-llm")

        # Set env vars — on Vercel, redirect tldextract cache to /tmp
        env = os.environ.copy()
        if IS_VERCEL:
            env["TLDEXTRACT_CACHE"] = "/tmp/.tldextract_cache"

        result = subprocess.run(
            cmd,
            cwd=str(PROJECT_DIR),
            capture_output=True,
            text=True,
            timeout=300,  # 5 min max
            env=env,
        )

        if os.path.exists(out_json):
            with open(out_json, "r") as f:
                result_dicts = json.load(f)
            return {"results": result_dicts, "error": None}
        else:
            stderr = result.stderr[:2000] if result.stderr else "No output produced"
            stdout = result.stdout[:2000] if result.stdout else ""
            return {
                "results": [],
                "error": f"Evaluation failed: {stderr}\n{stdout}".strip()
            }

    except subprocess.TimeoutExpired:
        return {"results": [], "error": "Evaluation timed out (5 min limit)"}
    except Exception as e:
        return {"results": [], "error": str(e)}
    finally:
        # Cleanup temp files
        for f in [tmp.name if tmp else None, out_json, out_md]:
            if f:
                try:
                    os.unlink(f)
                except OSError:
                    pass


@app.post("/api/evaluate")
async def evaluate(
    urls: str = Form(...),
    intended_use: str = Form("B"),
    use_llm: bool = Form(True),
):
    """
    Synchronous evaluation endpoint (works on Vercel serverless).
    Blocks until evaluation completes and returns results directly.
    """
    url_list = [u.strip() for u in urls.replace(",", "\n").split("\n") if u.strip()]
    url_list = [u for u in url_list if u.startswith("http")]

    if not url_list:
        return JSONResponse({"error": "No valid URLs provided"}, status_code=400)

    max_urls = VERCEL_MAX_URLS if IS_VERCEL else 200
    if len(url_list) > max_urls:
        return JSONResponse(
            {"error": f"Maximum {max_urls} URLs per batch"
                      + (" (serverless limit)" if IS_VERCEL else "")},
            status_code=400
        )

    # Run synchronously — blocks until done
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, _run_evaluation_sync, url_list, intended_use, use_llm
    )

    if result["error"]:
        return {
            "status": "error",
            "total": len(url_list),
            "completed": len(result["results"]),
            "results": result["results"],
            "error": result["error"],
        }

    return {
        "status": "done",
        "total": len(url_list),
        "completed": len(result["results"]),
        "results": result["results"],
        "error": None,
    }


# ── Legacy async endpoints (for local/Railway — NOT used on Vercel) ──

@app.post("/api/evaluate-async")
async def start_evaluation_async(
    urls: str = Form(...),
    intended_use: str = Form("B"),
    use_llm: bool = Form(True),
):
    """Start a source evaluation job asynchronously. Returns job_id immediately."""
    url_list = [u.strip() for u in urls.replace(",", "\n").split("\n") if u.strip()]
    url_list = [u for u in url_list if u.startswith("http")]

    if not url_list:
        return JSONResponse({"error": "No valid URLs provided"}, status_code=400)
    if len(url_list) > 200:
        return JSONResponse({"error": "Maximum 200 URLs per batch"}, status_code=400)

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "status": "running",
        "total": len(url_list),
        "completed": 0,
        "results": [],
        "started_at": time.time(),
        "error": None,
    }

    asyncio.get_event_loop().run_in_executor(
        None, _run_evaluation_job, job_id, url_list, intended_use, use_llm
    )

    return {"job_id": job_id, "total": len(url_list)}


def _run_evaluation_job(job_id: str, urls: list, intended_use: str, use_llm: bool):
    """Background job runner for async mode."""
    result = _run_evaluation_sync(urls, intended_use, use_llm)
    if result["error"]:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = result["error"]
    else:
        jobs[job_id]["results"] = result["results"]
        jobs[job_id]["completed"] = len(result["results"])
        jobs[job_id]["status"] = "done"


@app.get("/api/status/{job_id}")
async def job_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return {
        "status": job["status"],
        "total": job["total"],
        "completed": job["completed"],
        "error": job["error"],
    }


@app.get("/api/results/{job_id}")
async def job_results(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return {
        "status": job["status"],
        "total": job["total"],
        "completed": job["completed"],
        "results": job["results"],
        "error": job["error"],
    }


@app.post("/api/upload-file")
async def upload_file(file: UploadFile = File(...)):
    content = await file.read()
    text = content.decode("utf-8", errors="ignore")
    urls = [line.strip() for line in text.split("\n") if line.strip().startswith("http")]
    return {"urls": urls, "count": len(urls)}


if __name__ == "__main__":
    import uvicorn
    print("\n  Source Evaluator Web Interface")
    print(f"  Open http://localhost:8000 in your browser\n")
    uvicorn.run(app, host="0.0.0.0", port=8000)
