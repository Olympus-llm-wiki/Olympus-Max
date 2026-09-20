"""NED selection-sheet profile. Semantic section assignment precedes value parsing."""
from __future__ import annotations

from copy import deepcopy
import re

from . import PROFILE_VERSION
from .extract import WorkflowError

SYSTEM = re.compile(r"^[ПВ]+\d+(?:\.\d+)*$", re.I)
ASSUMPTIONS = {
    "Д1": {"title": "Мощность электронагревателя", "decision": "В T принята мощность нагрева потребляемая по расчётному подбору NED; установочная сохранена отдельно.",
           "limit": "Это рабочий смысл колонки, не нормативное требование. T не переносится автоматически в ведомость электрических нагрузок.",
           "urls": ["https://air-ned.com/tovar-7.html", "https://air-ned.com/tovar-15.html"]},
    "Д2": {"title": "Фильтры и обеззараживание", "decision": "«бак.сек.» трактуется как бактерицидная секция; AJ совмещает четвёртую ступень и дополнительную секцию по практике шаблона.",
           "limit": "Модель и параметры секции неизвестны; модель LB не выбирается по одному сокращению. Класс четвёртого фильтра сохраняется отдельно.",
           "urls": ["https://air-ned.com/tovar-19.html"]},
    "Д3": {"title": "Расчётная мощность охлаждения", "decision": "X содержит «Мощность расч. (кВт)» охладителя из подбора.",
           "limit": "Квалификация «полная/явная» не установлена; это не электрическая мощность ККБ.",
           "urls": ["https://air-ned.com/tovar-424.html", "https://air-ned.com/tovar-318.html"]},
}


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("−", "-").replace("³", "3").replace("º", "°").replace("°С", "°C")).strip()


def evidence(page: int, line: dict, source_sha: str) -> dict:
    return {"page": page, "bbox": line["bbox"], "quote": line["text"], "source_sha256": source_sha}


def _heading(text: str):
    text = normalize(text)
    if text == "ВЕНТИЛЯТОР":
        return "fan", 1, True
    match = re.match(r"^(НАГРЕВАТЕЛЬ|ОХЛАДИТЕЛЬ)\s+(\d+)$", text)
    if match:
        return ("heater" if match[1] == "НАГРЕВАТЕЛЬ" else "cooler"), int(match[2]), False
    match = re.match(r"^ФИЛЬТР\s+СТУПЕНЬ\s+(\d+)$", text)
    if match:
        return "filter", int(match[1]), False
    if text.startswith("АКУСТИЧЕСКИЕ ХАРАКТЕРИСТИКИ") or text.startswith("ПОДОБРАННАЯ АВТОМАТИКА"):
        return "stop", 0, True
    if text.startswith("СМЕШЕНИЕ"):
        return "stop", 0, False
    if re.match(r"^(ПАРОГЕНЕРАТОР|УВЛАЖНИТЕЛЬ|КОМПРЕССОРНО|РЕЗЕРВНЫЙ ДВИГАТЕЛЬ)", text):
        return "unsupported", 0, True
    return None


def parse_ned(extraction: dict) -> dict:
    all_text = "\n".join(p["text"] for p in extraction["pages"])
    if not all_text.strip():
        raise WorkflowError("needs_profile", {"reason": "no_text_layer", "route": "OCR or visual extraction"})
    if "Наименование установки" not in all_text or not re.search(r"ND\d+-\d+", all_text):
        raise WorkflowError("needs_profile", {"reason": "not_ned_selection"})
    systems = {}
    current = None
    direction = "supply"
    active = {"left": None, "right": None}
    issues = []
    for page in extraction["pages"]:
        match = re.search(r"Наименование\s+установки\s+([ПВ]+\d+(?:\.\d+)*)", page["text"], re.I)
        if match:
            name = match[1].upper()
            if name != current:
                current = name
                direction = "supply"
                active = {"left": None, "right": None}
            systems.setdefault(name, {"system": name, "pages": [], "blocks": {}, "model_candidates": [], "bactericidal": []})
        if current is None:
            continue
        system = systems[current]
        system["pages"].append(page["number"])
        # The label is vertically centred beside a multiline value; its first line
        # can precede «Тип установки» in reading order. Bound the complete header cell.
        model = re.search(r"Наименование\s+установки[^\n]*\n(.*?)Дата\s+коммерческого\s+предложения", page["text"], re.S)
        if model and "Тип установки" in normalize(model[1]):
            value = re.sub(r"-\s+(?=\d)", "-", normalize(normalize(model[1]).replace("Тип установки", "")))
            if not re.match(r"(?:AIRNED|LITENED|KVR|VRN)\b|(?:AIRNED|LITENED)-", value):
                issues.append({"code": "unknown_model_family", "system": current, "page": page["number"]})
            system["model_candidates"].append({"value": value, "unit": None, "evidence": [{"page": page["number"], "bbox": [0, 0, page["width"], min(page["height"], 220)], "quote": model[0], "source_sha256": extraction["source_sha256"]}]})
        if match and re.search(r"Наименование\s+установки[^\n]*бак\.?\s*сек", page["text"], re.I):
            quote = next((l for l in page["lines"] if "Наименование" in l["text"] and "бак" in l["text"]), {"text": match[0] + " + бак.сек.", "bbox": [0, 0, page["width"], 160]})
            system["bactericidal"].append(evidence(page["number"], quote, extraction["source_sha256"]))
        events = []
        for line in page["lines"]:
            if normalize(line["text"]) in ("Приточная часть", "Вытяжная часть"):
                events.append((line["bbox"][1] - .2, 0, "direction", line))
        for side in ("left", "right"):
            for line in page[side]:
                events.append((line["bbox"][1], 1, side, line))
        for _, _, side, line in sorted(events, key=lambda x: (x[0], x[1], x[2])):
            if side == "direction":
                next_direction = "exhaust" if "Вытяжная" in line["text"] else "supply"
                if next_direction != direction:
                    active = {"left": None, "right": None}
                direction = next_direction
                continue
            heading = _heading(line["text"])
            if heading:
                kind, index, wide = heading
                if kind == "unsupported":
                    issues.append({"code": "unsupported_component", "system": current, "page": page["number"], "heading": line["text"]})
                    active = {"left": None, "right": None}
                    continue
                affected = ("left", "right") if wide else (side,)
                key = f"{direction}:{kind}:{index}"
                if kind != "stop":
                    block = system["blocks"].setdefault(key, {"kind": kind, "index": index, "direction": direction, "lines": [], "heading_pages": []})
                    if page["number"] in block["heading_pages"]:
                        issues.append({"code": "repeated_component_header", "system": current, "page": page["number"], "component": key})
                    block["heading_pages"].append(page["number"])
                for col in affected:
                    active[col] = None if kind == "stop" else key
                continue
            if active[side] is not None:
                system["blocks"][active[side]]["lines"].append({**line, "page": page["number"]})
    if not systems:
        raise WorkflowError("needs_profile", {"reason": "no_system_headers"})
    return {"profile": PROFILE_VERSION, "source_sha256": extraction["source_sha256"], "systems": list(systems.values()), "issues": issues}


def choose(candidates: list[dict], context: dict, issues: list[dict]):
    distinct = {str(c["value"]) for c in candidates}
    if len(distinct) > 1:
        issues.append({"code": "conflicting_values", **context, "candidates": candidates})
        return None
    if not candidates:
        return None
    result = deepcopy(candidates[0])
    result["status"] = "supported"
    return result


def field(blocks: list[dict], pattern: str, unit: str | None, source_sha: str, context: dict, issues: list[dict], *, text=False):
    candidates = []
    for block in blocks:
        for line in block["lines"]:
            content = normalize(line["text"])
            match = re.match(pattern, content, re.I)
            if not match:
                continue
            if text:
                value = content[match.end():].strip()
                if not value:
                    continue
            else:
                suffix = content[match.end():].strip()
                if unit and unit.lower() not in normalize(content).lower():
                    issues.append({"code": "unexpected_unit", **context, "evidence": evidence(line["page"], line, source_sha)})
                    continue
                if ")" in suffix:
                    suffix = suffix.rsplit(")", 1)[1].strip()
                number = re.match(r"^([-+]?\d+(?:[.,]\d+)?)(?=\s|/|$)", suffix)
                if not number:
                    issues.append({"code": "invalid_number", **context, "evidence": evidence(line["page"], line, source_sha)})
                    continue
                value = float(number[1].replace(",", "."))
                if value.is_integer():
                    value = int(value)
            candidates.append({"value": value, "unit": unit, "evidence": [evidence(line["page"], line, source_sha)]})
    return choose(candidates, context, issues)


def system_fields(system: dict, direction: str, source_sha: str, issues: list[dict]) -> dict:
    """One row worth of source facts, with no project-specific constants."""
    blocks = [b for b in system["blocks"].values() if b["direction"] == direction]
    fans = [b for b in blocks if b["kind"] == "fan"]
    heaters = [b for b in blocks if b["kind"] == "heater"]
    water = [b for b in heaters if any("Тип теплоносителя" in l["text"] for l in b["lines"])]
    electric = [b for b in heaters if any("Мощность нагрева установочная" in l["text"] for l in b["lines"])]
    coolers = [b for b in blocks if b["kind"] == "cooler"]
    context = {"system": system["system"], "direction": direction}
    result = {}

    def put(key, collection, pattern, unit=None, text=False):
        if len(collection) > 1:
            issues.append({"code": "multiple_components", **context, "field": key})
            return
        value = field(collection, pattern, unit, source_sha, {**context, "field": key}, issues, text=text)
        if value is not None:
            result[key] = value

    for key, pattern, unit in [
        ("G", r"Расход воздуха", "м3/ч"), ("J", r"P\s+свободное", "Па"),
        ("K", r"(?:Номинальная|Установочная) мощность", "кВт"), ("L", r"Напряжение", "В"),
    ]:
        put(key, fans, pattern, unit)
    count = field(fans, r"Количество агрегатов", "шт", source_sha, {**context, "field": "fan_count"}, issues)
    if count is not None and count["value"] != 1:
        result.pop("K", None)
        issues.append({"code": "multiple_fan_units", **context, "count": count})
    for col, coll, pattern, unit in [
        ("N", water, r"t°\s*/?\s*влажность вх\. воздуха", "°C"),
        ("O", water, r"t°\s*/?\s*влажность вых\. воздуха", "°C"),
        ("P", water, r"Мощность нагрева потребляемая", "кВт"),
        ("R", electric, r"t°\s*/?\s*влажность вх\. воздуха", "°C"),
        ("S", electric, r"t°\s*/?\s*влажность вых\. воздуха", "°C"),
        ("T", electric, r"Мощность нагрева потребляемая", "кВт"),
        ("T_installed", electric, r"Мощность нагрева установочная", "кВт"),
        ("U", electric, r"Напряжение/Число ступеней", None),
        ("V", coolers, r"t°\s*вх\. воздуха", "°C"),
        ("W", coolers, r"t°\s*вых\. воздуха", "°C"),
        ("X", coolers, r"Мощность расч\.", "кВт"),
    ]:
        put(col, coll, pattern, unit)
    put("Y", coolers, r"Тип хладагента/хладоносителя", text=True)
    fluid = field(water, r"Тип теплоносителя", None, source_sha, context, issues, text=True)
    tin = field(water, r"t°\s*вх\. жидкости", "°C", source_sha, context, issues)
    tout = field(water, r"t°\s*вых\. жидкости", "°C", source_sha, context, issues)
    if fluid and tin and tout:
        result["Q"] = {"value": f"{fluid['value']}; {tin['value']}/{tout['value']} °C", "unit": None, "status": "supported", "evidence": fluid["evidence"] + tin["evidence"] + tout["evidence"]}
    for index, col in enumerate(("AG", "AH", "AI", "AJ"), 1):
        put(col, [b for b in blocks if b["kind"] == "filter" and b["index"] == index], r"Класс очистки", text=True)
    if any(b["kind"] == "filter" and b["index"] > 4 for b in blocks):
        issues.append({"code": "too_many_filter_stages", **context})
    if direction == "supply" and system["bactericidal"]:
        existing = result.get("AJ")
        result["AJ"] = {"value": ((str(existing["value"]) + " (4-я ступень); ") if existing else "") + "Бактерицидная секция*",
                        "unit": None, "status": "assumption", "assumption_id": "Д2",
                        "evidence": (existing["evidence"] if existing else []) + system["bactericidal"][:1]}
    for col, assumption in (("T", "Д1"), ("X", "Д3")):
        if col in result:
            result[col].update(status="assumption", assumption_id=assumption)
    for col in ("G", "J", "K", "L"):
        if col not in result:
            issues.append({"code": "missing_core_field", **context, "field": col})
    for collection, required in ((water, ("N", "O", "P", "Q")), (electric, ("R", "S", "T", "T_installed", "U")), (coolers, ("V", "W", "X", "Y"))):
        if collection:
            for col in required:
                if col not in result:
                    issues.append({"code": "missing_component_field", **context, "field": col})
    return result
