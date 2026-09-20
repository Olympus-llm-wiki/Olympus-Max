from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys

from . import PROFILE_VERSION
from .extract import WorkflowError
from ..preservation import PreservationError


def parser():
    p = argparse.ArgumentParser(prog="khvs", description="Локальный подбор NED → ХОВС с источниками и рабочими допущениями.")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Проверить локальные зависимости без записи в Store")
    sub.add_parser("profiles", help="Показать поддержанные профили")
    run = sub.add_parser("run", help="Создать новый локальный комплект результата")
    run.add_argument("--pdf", required=True)
    run.add_argument("--template", required=True)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--state", type=Path, default=Path(os.environ.get("OLYMPUS_STATE_DIR", str(Path.home() / ".local/state/olympus"))))
    run.add_argument("--cache", type=Path)
    run.add_argument("--profile", default=PROFILE_VERSION)
    run.add_argument("--no-preview", action="store_true", help="Не рендерить XLSX-превью для программного прогона")
    page = sub.add_parser("page", help="Показать страницу или область проверенного снимка исходника")
    page.add_argument("job", type=Path)
    page.add_argument("--page", type=int, required=True)
    page.add_argument("--bbox", type=float, nargs=4, metavar=("X0", "TOP", "X1", "BOTTOM"))
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "profiles":
            result = {"profiles": [{"id": PROFILE_VERSION, "input": "Текстовый подбор NED и пустая область параметров ХОВС", "unsupported": ["SHUFT", "сканы", "DWG", "инженерные расчёты"]}]}
        elif args.command == "doctor":
            from .workbook import runtime_paths
            paths = runtime_paths()
            missing = [name for name in ("pdfplumber", "pypdf", "reportlab", "openpyxl", "lxml") if importlib.util.find_spec(name) is None]
            if not paths["node"].is_file():
                missing.append("node")
            if not (paths["modules"] / "@oai/artifact-tool").is_dir():
                missing.append("@oai/artifact-tool")
            result = {"ready": not missing, "missing": missing, "python": sys.executable, "node": str(paths["node"])}
        elif args.command == "run":
            from .pipeline import resolve_input, run
            result = run(resolve_input(args.pdf), resolve_input(args.template), args.out, args.state, args.cache, profile=args.profile, preview=not args.no_preview)
        else:
            from .pipeline import render_page
            result = render_page(args.job, args.page, args.bbox)
        print(json.dumps(result, ensure_ascii=False))
        return 2 if result.get("status") == "needs_review" or result.get("ready") is False else 0
    except WorkflowError as error:
        print(json.dumps({"status": "needs_profile" if error.code == "needs_profile" else "error", "code": error.code, "details": error.details}, ensure_ascii=False))
        return 2
    except PreservationError as error:
        print(json.dumps({"status": "error", "code": str(error)}, ensure_ascii=False))
        return 2
    except (ImportError, OSError, ValueError) as error:
        print(json.dumps({"status": "error", "code": type(error).__name__, "message": str(error)}, ensure_ascii=False))
        return 2
    except Exception as error:
        print(json.dumps({"status": "error", "code": type(error).__name__, "message": "Сбой локального шага; сведения о сохранённых источниках и код ошибки находятся в failure.json, если каталог задания создан."}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
