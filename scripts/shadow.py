"""Теневой прогон заявок: папка писем + прайс → отчёт по качеству разбора.

Ничего не отправляет, никуда не пишет, боевую базу не трогает: свой временный
каталог данных на прогон. Нужен, чтобы получить честные цифры на письмах
реального клиента до продажи, а не на демо-наборе из 8 писем.

    python scripts/shadow.py --letters C:\\letters --price C:\\price.xlsx
    python scripts/shadow.py --letters data\\samples --no-llm
    python scripts/shadow.py --letters C:\\letters --json report.json

Метрики: доля писем-заявок, строк разобрано без человека, «не найдено»,
спорные, время на письмо. Ровно те цифры, которые нужны для пилота.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Теневой прогон писем через kpbot")
    parser.add_argument("--letters", required=True, type=Path, help="папка с .eml")
    parser.add_argument("--price", type=Path, default=BASE_DIR / "data" / "price_list.xlsx",
                        help="прайс .xlsx/.csv (по умолчанию демо-прайс)")
    parser.add_argument("--no-llm", action="store_true", help="только правила, без модели")
    parser.add_argument("--json", type=Path, help="куда сохранить машинный отчёт")
    parser.add_argument("--limit", type=int, default=0, help="сколько писем взять (0 — все)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    letters = sorted(args.letters.glob("*.eml"))
    if not letters:
        print(f"в {args.letters} нет .eml")
        return 2
    if args.limit:
        letters = letters[: args.limit]

    # Изолированный каталог: боевые data/ и .env-настройки каналов не используются.
    # Не tempfile: в песочнице Windows такой каталог получает ACL без доступа.
    run_dir = BASE_DIR / ".tmp" / f"shadow-{os.getpid()}-{time.time_ns()}"
    run_dir.mkdir(parents=True, exist_ok=True)
    os.environ["KPBOT_DATA_DIR"] = str(run_dir)
    os.environ["KPBOT_ENV"] = "dev"
    os.environ["KPBOT_PDF_ENABLED"] = "false"
    os.environ["KPBOT_TELEGRAM_ENABLED"] = "false"
    os.environ["KPBOT_MAX_ENABLED"] = "false"
    os.environ["KPBOT_SMTP_ENABLED"] = "false"
    if args.no_llm:
        os.environ["KPBOT_LLM_ENABLED"] = "false"

    sys.path.insert(0, str(BASE_DIR))
    from kpbot.config import settings
    from kpbot.mailparse import parse_eml
    from kpbot.pipeline import Pipeline
    from kpbot.pricelist import load_price_items
    from kpbot.store import Store

    store = Store(settings.db_path)
    store.init_schema()
    store.replace_price_items(load_price_items(args.price))
    pipeline = Pipeline(store, settings)

    rows = []
    for path in letters:
        started = time.perf_counter()
        try:
            parsed = parse_eml(path.read_bytes())
            lines, quote, is_request = pipeline.analyze(parsed.text)
        except Exception as exc:  # noqa: BLE001 - одно плохое письмо не рвёт отчёт
            rows.append({"file": path.name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        counts = {"ok": 0, "ambiguous": 0, "no_match": 0, "invalid": 0}
        for line in lines:
            counts[line.status.value] = counts.get(line.status.value, 0) + 1
        rows.append({
            "file": path.name,
            "is_request": is_request,
            "lines": len(lines),
            "priced": len(quote.lines) if quote else 0,
            "total": str(quote.total) if quote else "0.00",
            "needs_human": sum(counts.values()) - counts["ok"],
            "elapsed_ms": elapsed_ms,
            **counts,
        })

    print(f"\nтеневой прогон · писем {len(rows)} · модель {'выкл' if args.no_llm else 'вкл'}")
    print(f"{'письмо':34} {'заявка':7} {'строк':6} {'в расчёт':9} {'спорн':6} {'мс':6} {'сумма':>12}")
    print("-" * 92)
    for row in rows:
        if "error" in row:
            print(f"{row['file'][:33]:34} ошибка: {row['error'][:48]}")
            continue
        print(f"{row['file'][:33]:34} {str(row['is_request']):7} {row['lines']:6} "
              f"{row['priced']:9} {row['needs_human']:6} {row['elapsed_ms']:6} "
              f"{row['total']:>12}")

    good = [r for r in rows if "error" not in r]
    requests = [r for r in good if r["is_request"]]
    total_lines = sum(r["lines"] for r in requests)
    total_priced = sum(r["priced"] for r in requests)
    ms = [r["elapsed_ms"] for r in good] or [0]
    summary = {
        "letters": len(good),
        "requests": len(requests),
        "lines": total_lines,
        "priced": total_priced,
        "auto_share": round(total_priced / total_lines, 3) if total_lines else 0.0,
        "median_ms": int(statistics.median(ms)),
        "model": "off" if args.no_llm else "on",
    }
    print("-" * 92)
    print(f"заявок {summary['requests']} из {summary['letters']} · строк {summary['lines']} · "
          f"в расчёт без человека {summary['priced']} ({summary['auto_share']:.0%}) · "
          f"медиана {summary['median_ms']} мс")
    print(f"временный каталог: {run_dir}")

    if args.json:
        args.json.write_text(json.dumps({"summary": summary, "rows": rows},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"отчёт: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
