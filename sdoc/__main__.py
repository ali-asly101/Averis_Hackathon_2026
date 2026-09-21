"""
python -m sdoc <command>

  check                         is everything set up? (.env, key, data, OCR, UI)
  run    [--no-ai] [--retry] [--limit N] [--submit] [--data X]
                                process the inbox -> output/
  score  [submission] [--gt ground_truth.json] [--show N]
                                score locally against a ground truth file
  review list | show ID | confirm ID | correct ID --si f=v --bl f=v
         | override ID [--category C] [--status S] [--defect f ...] [--reason R]
         | reopen ID            human review from the terminal
  serve  [--reload]             web API + UI (http://SDOC_API_HOST:SDOC_API_PORT)
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import config


def cmd_check(_args):
    ok = True
    info = config.describe()
    print("Settings (from .env / environment):")
    for k, v in info.items():
        print(f"  {k:18s} {v}")
    print("\nChecks:")

    def line(passed, text, fix=""):
        nonlocal ok
        ok = ok and passed
        print(f"  [{'ok' if passed else '!!'}] {text}" + (f"\n       -> {fix}" if fix and not passed else ""))

    line(sys.version_info >= (3, 10), f"Python {sys.version.split()[0]}", "use Python 3.10+")
    missing = []
    for mod, pkg in [("pdfplumber", "pdfplumber"), ("openpyxl", "openpyxl"), ("docx", "python-docx"),
                     ("PIL", "Pillow"), ("fastapi", "fastapi"), ("uvicorn", "uvicorn"), ("openai", "openai")]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    line(not missing, "Python packages installed", f"pip install -r requirements.txt  (missing: {', '.join(missing)})")
    line((config.ROOT / ".env").is_file(), ".env file present", "copy .env.example to .env")
    has_key = bool(config.llm_api_key())
    line(has_key or config.ai_disabled(), "AI key set" if has_key else "AI key",
         "put LLM_API_KEY=... in .env (or run with --no-ai: rules + OCR only)")

    from . import ocr
    engine = ocr.available_engine()
    line(engine is not None, f"OCR engine: {engine or 'none'}", "pip install rapidocr_onnxruntime")

    try:
        from .loader import Inbox
        if not config.data_is_remote() and not (Path(config.data_source()) / "inbox").is_dir():
            raise FileNotFoundError(f"no inbox/ folder in {config.data_source()}")
        n = len(Inbox(config.data_source()).emails())
        line(n > 0, f"data reachable: {n} emails at {config.data_source()}",
             "set SDOC_DATA in .env to your data folder or http://localhost:8080")
    except Exception as e:
        line(False, f"data NOT reachable at {config.data_source()} ({type(e).__name__})",
             "put the data folder (inbox/ + attachments/) in the project root as data_v2, set SDOC_DATA "
             "in .env, or start the organizers' server (docker compose up) and use http://localhost:8080")

    dist = config.ROOT / "frontend" / "dist" / "index.html"
    line(dist.is_file(), "web UI built", "cd frontend && npm install && npm run build")
    line(shutil.which("npm") is not None, "npm available (only needed to build/develop the UI)",
         "install Node.js 20+ from nodejs.org")
    print("\nAll good." if ok else "\nFix the [!!] items above.")
    return 0 if ok else 1


def cmd_run(args):
    from . import pipeline
    from .loader import Inbox
    if args.no_ai:
        os.environ["SDOC_DISABLE_AI"] = "1"
    if args.data:
        os.environ["SDOC_DATA"] = args.data
    pipeline.print_banner()
    submission, _ = pipeline.run(limit=args.limit, retry=args.retry)
    print(f"\nWrote {config.out_dir()}/submission.json, report.md, review_queue.json")
    if args.submit:
        if not config.data_is_remote():
            print("--submit needs the organizers' server: set SDOC_DATA=http://localhost:8080")
            return 1
        print("\nScoreboard from the server:")
        print(json.dumps(Inbox(config.data_source()).submit(submission), indent=2))
    elif config.ground_truth_path():
        print("Score it:  python -m sdoc score")
    return 0


def cmd_score(args):
    from . import scoring
    sub_path = Path(args.submission) if args.submission else config.out_dir() / "submission.json"
    gt_path = Path(args.gt) if args.gt else config.ground_truth_path()
    if not sub_path.is_file():
        print(f"No submission at {sub_path} - run `python -m sdoc run` first")
        return 1
    if not gt_path or not Path(gt_path).is_file():
        print("No ground_truth.json available locally. Score against the organizers' server instead:\n"
              "  set SDOC_DATA=http://localhost:8080 in .env, then: python -m sdoc run --submit")
        return 1
    sub = json.loads(sub_path.read_text(encoding="utf-8"))
    gt = json.loads(Path(gt_path).read_text(encoding="utf-8"))
    print(json.dumps(scoring.score(sub, gt), indent=2))
    for k, got, exp in scoring.wrong_rows(sub, gt)[:args.show]:
        print(f"  {k}: got {got}  expected {exp}")
    return 0


def _kv(pairs):
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise ValueError(f"Expected field=value, got {p!r}")
        k, v = p.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def cmd_review(args):
    from . import review
    out = config.out_dir()
    try:
        if args.action == "list":
            for q in review.queue(out):
                print(f"{q['email_id']}  {q['status']}/{q['review_reason']}  "
                      f"proposed={q['proposed']}  {q['why']}")
            return 0
        if not args.email_id:
            raise review.ReviewError("email_id required")
        if args.action == "show":
            print(json.dumps(review.get_case(out, args.email_id), indent=2,
                             ensure_ascii=False, default=str)[:8000])
            return 0
        d = review.decide(out, args.email_id, args.action, reviewer=args.by, note=args.note,
                          si_fields=_kv(args.si), bl_fields=_kv(args.bl), category=args.category,
                          status=args.status, defect_fields=args.defect, review_reason=args.reason)
        print("reopened" if d is None else f"saved: {d['final']}")
        return 0
    except (review.ReviewError, ValueError) as e:
        print(f"error: {e}")
        return 2


def cmd_serve(args):
    import uvicorn
    print(f"SDOC web API on http://{config.api_host()}:{config.api_port()}")
    if (config.ROOT / "frontend" / "dist" / "index.html").is_file():
        print(f"UI:               http://{config.api_host()}:{config.api_port()}/")
    else:
        print("UI not built: `cd frontend && npm run build`, or run `npm run dev` for development")
    uvicorn.run("sdoc.api:app", host=config.api_host(), port=config.api_port(), reload=args.reload)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m sdoc", description="Shipping document verification")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="verify setup").set_defaults(fn=cmd_check)

    r = sub.add_parser("run", help="process the inbox")
    r.add_argument("--data", help="override SDOC_DATA (folder or server URL)")
    r.add_argument("--no-ai", action="store_true", help="rules + OCR only, no AI calls")
    r.add_argument("--retry", action="store_true", help="re-run only emails that failed retryably")
    r.add_argument("--limit", type=int, help="only the first N emails")
    r.add_argument("--submit", action="store_true", help="POST the result to the organizers' server")
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("score", help="score locally against ground_truth.json")
    s.add_argument("submission", nargs="?")
    s.add_argument("--gt", help="ground truth file (default: SDOC_GROUND_TRUTH or <data>/ground_truth.json)")
    s.add_argument("--show", type=int, default=10, help="print up to N wrong rows")
    s.set_defaults(fn=cmd_score)

    v = sub.add_parser("review", help="human review from the terminal")
    v.add_argument("action", choices=["list", "show", "confirm", "correct", "override", "reopen"])
    v.add_argument("email_id", nargs="?")
    v.add_argument("--by"); v.add_argument("--note")
    v.add_argument("--si", nargs="*", help="field=value corrections for the SI")
    v.add_argument("--bl", nargs="*", help="field=value corrections for the BL")
    v.add_argument("--category"); v.add_argument("--status")
    v.add_argument("--defect", nargs="*"); v.add_argument("--reason")
    v.set_defaults(fn=cmd_review)

    w = sub.add_parser("serve", help="web API + UI")
    w.add_argument("--reload", action="store_true", help="auto-reload on code changes (dev)")
    w.set_defaults(fn=cmd_serve)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
