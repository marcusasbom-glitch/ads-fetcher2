# i webapi.py
# ...
async def do_work(job_id: str, ar_input: str, email: str | None):
    job_dir = RUNS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    status_path = job_dir / "status.json"

    def write_status(obj):
        status_path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")

    write_status({"status": "running", "result_url": None})

    try:
        # Kör direkt i jobbkatalogen
        # (vi behöver inte byta cwd längre – funktionerna tar run_dir)
        # 1) Capture
        await capture_network(ar_input, run_dir=job_dir)

        # 2) Process
        ok = process_candidates_and_save(run_dir=job_dir)
        if not ok:
            write_status({"status": "error", "error": "processing_failed"})
            return

        write_status({"status": "done"})
    except Exception as e:
        write_status({"status": "error", "error": str(e)})
# ...
