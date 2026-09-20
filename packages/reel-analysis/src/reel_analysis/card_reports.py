"""Deterministic, portable views of cards; no media or model calls."""
from __future__ import annotations

import html
import json
from pathlib import Path

from .card_sources import child, dependencies_changed, load_corpus, output_root
from .common import ReelError, digest, file_hash, read_json, write_json
from .content_cards import assembled, checked, publish_card, seal
from .store import file_lock

LABELS = {
    "concept": "Понятие", "step": "Шаг", "demonstration": "Демонстрация",
    "example": "Пример", "caveat": "Оговорка", "conclusion": "Вывод",
    "hook": "Хук", "promise": "Обещание", "proof": "Доказательство",
    "body": "Основная часть", "payoff": "Результат", "cta": "Призыв к действию", "delivery": "Подача",
}
VIEWS = ("transcript", "notes", "editing", "comparison")


def safe(text):
    text = html.escape(str(text), quote=False)
    for char in "\\*_{}[]()#+-.!|" + chr(96):
        text = text.replace(char, "\\" + char)
    return text.replace("\r", "").replace("\n", " ")


def stamp(start, end):
    def one(t):
        if t is None:
            return "?"
        return f"{int(t // 60):02d}:{t % 60:06.3f}"
    return one(start) + "–" + one(end)


def anchor(value):
    return "e-" + digest(value)[:20]


def cite(item_id, event):
    target = f"sources/{item_id}.md#" + anchor(event["id"])
    return f"[{safe(event['id'])} · {stamp(event['start_s'], event['end_s'])}]({target})"


def point_text(card, point):
    known = {e["id"]: e for e in card["source"]["events"]}
    links = " ".join(cite(card["id"], known[ref]) for ref in point["evidence_ids"])
    return f"**{safe(point['title'])}** — {safe(point['text'])} {links}"


def evidence_page(card):
    out = [f"# Наблюдения: {safe(card['title'])}", "", "Исходные машинные наблюдения; речь и времена не аттестованы.", ""]
    for event in card["source"]["events"]:
        out += [f'<a id="{anchor(event["id"])}"></a>', f"## {safe(event['id'])}", "",
                f"{safe(event['track'])}; {stamp(event['start_s'], event['end_s'])}; {safe(event['variant'])}", ""]
        out += ["    " + line for line in json.dumps(event["observation"], ensure_ascii=False, indent=2).splitlines()]
        out += [""]
    return "\n".join(out)


def extraction_summary(source):
    if source["format"] == "media":
        accepted = {x["part"] for x in source["quality_notes"] if isinstance(x, dict) and "part" in x}
        missing = {x["part"] for x in source["gaps"] if isinstance(x, dict) and x.get("reason") == "source_part_unavailable"}
        count = f"{len(accepted)}/{len(accepted | missing)}"
    else:
        count = "1/1"
    ranges = [stamp(e["start_s"], e["end_s"]) for e in source["transcript"] if e["start_s"] is not None]
    text = f"Исходное извлечение: {count} частей; покрытие: {source['coverage']}."
    if source["format"] == "media" and ranges:
        text += " Границы доступной речи по частям: " + ", ".join(ranges) + "."
    return text


def render(cards, index, view):
    observation_view = view in ("transcript", "editing")
    out = [f"# {safe(index['corpus_id'])}: {view}", "",
           "Качество: черновик. Времена приблизительные; совпадение ссылок не доказывает правильность выводов.", "",
           f"В описи: {len(index['inventory'])}; выбрано: {len(index['selected_ids'])}.", "",
           "| Материал | Выбран | " + ("Источник" if observation_view else "Карточка") + " |", "|---|---|---|"]
    by_id = {c["id"]: c for c in cards}
    for item in index["inventory"]:
        state = (by_id[item["id"]]["source"]["coverage"] if observation_view else by_id[item["id"]]["state"]) if item["selected"] else "не выбран"
        out.append(f"| {safe(item['id'])}: {safe(item['title'])} | {'да' if item['selected'] else 'нет'} | {state} |")
    out += [""]
    if view == "comparison":
        kinds = list(dict.fromkeys(p["kind"] for c in cards for p in c["points"]))
        for kind in kinds:
            out += [f"## {LABELS[kind]}", "", "| Материал | Наблюдения и интерпретации |", "|---|---|"]
            for card in cards:
                points = [point_text(card, p) for p in card["points"] if p["kind"] == kind]
                out.append(f"| {safe(card['title'])} | {'<br>'.join(points) if points else 'Не описано в карточке; отсутствие приёма не установлено'} |")
            out += [""]
    else:
        for card in cards:
            out += [f"## {safe(card['title'])}", "", extraction_summary(card["source"]), ""]
            if not observation_view:
                out += [f"Смысловой профиль: {card['profile']}; карточка: {card['state']}.", ""]
            if view == "transcript":
                out += ["Сохранённая речь без смыслового переписывания. Стыки и повторы остаются.", ""]
                for entry in card["source"]["transcript"]:
                    out += [f"### {stamp(entry['start_s'], entry['end_s'])} · {safe(entry['label'])}", ""]
                    out += ["    " + line for line in entry["text"].splitlines()] + [""]
                if not card["source"]["transcript"]:
                    out += ["Сохранённая речь отсутствует. Причина и доступность модальности — в ограничениях источника.", ""]
            elif view == "editing":
                for event in card["source"]["events"]:
                    if event["track"] != "speech":
                        out += [cite(card["id"], event), ""]
                        out += ["    " + line for line in json.dumps(event["observation"], ensure_ascii=False).splitlines()] + [""]
            else:
                for point in card["points"]:
                    out += [f"### {LABELS[point['kind']]}", "", point_text(card, point), ""]
                if not card["points"]:
                    out += ["Смысловые пункты ещё не подготовлены; доступные данные сохранены в источнике.", ""]
    out += ["## Покрытие и ограничения", ""]
    for card in cards:
        out += [f"### {safe(card['title'])}", "", extraction_summary(card["source"]), "",
                f"[Полный snapshot](cards/{card['id']}.json) · [Наблюдения](sources/{card['id']}.md)", ""]
        if observation_view:
            out += ["Экспортированы доступные исходные наблюдения. Создание смысловой карточки для этого представления не требуется.", ""]
        else:
            out += [f"Смысловые части карточки: {card['coverage']['ready_parts']}/{card['coverage']['total_parts']}.", ""]
        gaps = card["source"]["gaps"] if observation_view else card["gaps"]
        missing = sorted({g["part"] + 1 for g in gaps if isinstance(g, dict) and g.get("reason") == "source_part_unavailable"})
        if missing:
            out += ["- Недоступные исходные части: " + ", ".join(map(str, missing)) + ". Точные интервалы сохранены в snapshot."]
        for gap in gaps:
            if isinstance(gap, dict) and gap.get("reason") == "source_part_unavailable":
                continue
            out += ["- " + safe(json.dumps(gap, ensure_ascii=False) if not isinstance(gap, str) else gap)]
        out += [""]
    return "\n".join(out)


def export_report(output, target, view="notes"):
    if view not in VIEWS:
        raise ReelError("content_unknown_view")
    root = Path(output).expanduser().absolute()
    target = Path(target).expanduser().absolute()
    if target.exists():
        raise ReelError("content_report_target_exists")
    output_root(target.parent)
    with file_lock(root / ".content.lock", blocking=False):
        index = read_json(root / "selection.json")
        cards, bindings = [], []
        for row in index["inventory"]:
            if not row["selected"]:
                continue
            card, pointer = publish_card(root, row)
            if dependencies_changed(card["source"]):
                raise ReelError("content_report_source_stale_replan")
            cards.append(card)
            version = child(root, row["version"])
            bindings.append({
                "id": row["id"], "fingerprint": row["fingerprint"],
                "current_path": str(child(root, f"items/{row['id']}/current.json")),
                "card_current_path": str(version / "card-current.json"),
                "card_pointer": pointer, "version": row["version"],
            })
        target.mkdir(parents=True)
        names = []
        for card in cards:
            filename = f"cards/{card['id']}.json"
            write_json(target / filename, card)
            names.append(filename)
            page = target / "sources" / f"{card['id']}.md"
            page.parent.mkdir(exist_ok=True)
            page.write_text(evidence_page(card))
            names.append(str(page.relative_to(target)))
        snapshot = {"schema_version": 1, "view": view, "index": index, "bindings": bindings,
                    "source_output": str(root), "quality": "draft_unverified"}
        write_json(target / "report.json", snapshot)
        (target / "report.md").write_text(render(cards, index, view))
        names.extend(["report.json", "report.md"])
        seal(target, names)
    return {"directory": str(target), "markdown": str(target / "report.md"), "view": view, "items": len(cards)}


def check_report(target, manifest_path=None, output=None):
    target = Path(target).expanduser().absolute()
    try:
        snapshot = checked(target, "report.json")
        cards = {row["id"]: checked(target, f"cards/{row['id']}.json") for row in snapshot["bindings"]}
    except (ReelError, OSError, ValueError, KeyError, TypeError):
        return {"state": "corrupt", "issues": [{"reason": "report_integrity_mismatch"}]}
    issues = []
    index = snapshot["index"]
    manifest_path = Path(manifest_path or index["manifest_path"])
    if not manifest_path.is_file():
        issues.append({"reason": "manifest_missing"})
    else:
        try:
            current = load_corpus(manifest_path)
            if digest(current) != index["selection_digest"]:
                issues.append({"reason": "selection_changed", "selected_ids": current["selected_ids"]})
        except (ValueError, ReelError, OSError):
            issues.append({"reason": "manifest_invalid"})
    for binding in snapshot["bindings"]:
        item_id = binding["id"]
        for issue in dependencies_changed(cards[item_id]["source"]):
            issues.append(dict(issue, id=item_id))
        current_path = Path(binding["current_path"])
        card_path = Path(binding["card_current_path"])
        if output:
            new_root = Path(output).absolute()
            current_path = new_root / "items" / item_id / "current.json"
            card_path = new_root / binding["version"] / "card-current.json"
        if not current_path.is_file() or not card_path.is_file():
            issues.append({"id": item_id, "reason": "card_current_missing"})
            continue
        try:
            current = read_json(current_path)
            pointer = read_json(card_path)
            if current["fingerprint"] != binding["fingerprint"] or pointer != binding["card_pointer"]:
                issues.append({"id": item_id, "reason": "card_version_changed"})
            root = Path(output or snapshot["source_output"])
            live_card = checked(child(root, pointer["path"]), "card.json")
            if digest(live_card) != pointer["card_version"]:
                issues.append({"id": item_id, "reason": "card_integrity_mismatch"})
        except (ReelError, OSError, ValueError, KeyError, TypeError):
            issues.append({"id": item_id, "reason": "card_integrity_mismatch"})
    state = "current"
    if issues:
        state = "corrupt" if any("integrity" in x["reason"] for x in issues) else "missing" if any("missing" in x["reason"] for x in issues) else "stale"
    return {"state": state, "issues": issues, "quality": "draft_unverified"}


def search_cards(output, query):
    if not query.strip():
        raise ReelError("content_query_required")
    root = Path(output).expanduser().absolute()
    with file_lock(root / ".content.lock", blocking=False):
        index = read_json(root / "selection.json")
        matches, warnings = [], []
        for row in index["inventory"]:
            if not row["selected"]:
                continue
            card = assembled(root, row)
            for issue in dependencies_changed(card["source"]):
                warnings.append(dict(issue, id=row["id"]))
            for point in card["points"]:
                if query.casefold() in (point["title"] + "\n" + point["text"]).casefold():
                    known = {e["id"]: e for e in card["source"]["events"]}
                    matches.append({"source_id": row["id"], "source_path": row["path"], "profile": row["profile"],
                                    "point": point, "evidence": [known[e] for e in point["evidence_ids"]],
                                    "fingerprint": row["fingerprint"], "quality": "draft_unverified"})
        return {"query": query, "method": "literal_casefold", "selected_ids": index["selected_ids"], "matches": matches, "warnings": warnings}


def add_parser(commands):
    content = commands.add_parser("content", help="cards and local views from retained observations")
    actions = content.add_subparsers(dest="content_command", required=True)
    for name in ("plan", "run"):
        p = actions.add_parser(name)
        p.add_argument("manifest")
        p.add_argument("--output", required=True)
        p.add_argument("--model", default="gemini-3.8-flash-high")
        p.add_argument("--part-bytes", type=int, default=48000)
        if name == "run":
            p.add_argument("--max-parts", type=int, default=1)
            p.add_argument("--retry-failed", action="store_true")
    p = actions.add_parser("report")
    p.add_argument("--output", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--view", choices=VIEWS, default="notes")
    p = actions.add_parser("check")
    p.add_argument("report")
    p.add_argument("--manifest")
    p.add_argument("--output")
    p = actions.add_parser("search")
    p.add_argument("--output", required=True)
    p.add_argument("--query", required=True)


def dispatch(args):
    from .content_cards import plan, run
    if args.content_command == "plan":
        return plan(args.manifest, args.output, args.model, args.part_bytes)
    if args.content_command == "run":
        return run(args.manifest, args.output, args.model, args.part_bytes, args.max_parts, args.retry_failed)
    if args.content_command == "report":
        return export_report(args.output, args.target, args.view)
    if args.content_command == "check":
        return check_report(args.report, args.manifest, args.output)
    return search_cards(args.output, args.query)
