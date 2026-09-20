"""Scoped XLSX authoring and preservation of properties outside approved edits."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import zipfile

from .extract import WorkflowError, write_json

RUNTIME = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies"


def runtime_paths() -> dict:
    node = Path(os.environ.get("KHVS_NODE", str(RUNTIME / "node/bin/node")))
    modules = Path(os.environ.get("KHVS_NODE_MODULES", str(RUNTIME / "node/node_modules")))
    return {"node": node, "modules": modules}


def configuration(schedule: dict, template: Path, work: Path) -> dict:
    values = {u["cell"]: u["value"] for u in schedule["updates"]}
    notes, fills = {}, {}
    for u in schedule["updates"]:
        ident = u.get("assumption_id")
        if ident:
            a = schedule["assumptions"][ident]
            notes[u["cell"]] = f"{ident} — рабочее допущение. {a['decision']} {a['limit']} Источник: физическая страница {u['page']} PDF."
            fills[u["cell"]] = "#FFF0D0"
    for p in schedule["electric_power"]:
        if p["cell"] in notes:
            notes[p["cell"]] += f" Потребляемая: {p['consumed_kw']} кВт; установочная: {p['installed_kw']} кВт."
    for cell, ident in (("T3", "Д1"), ("AJ4", "Д2"), ("X3", "Д3")):
        a = schedule["assumptions"][ident]
        notes[cell] = f"{ident}: {a['decision']} {a['limit']}"
        fills[cell] = "#FFF0D0"
    rows = [row for system in schedule["systems"] for row in system["rows"]]
    for row in rows:
        for col in ("M", "Z", "AA", "AB", "AC", "AD", "AE", "AF"):
            cell = f"{col}{row}"
            if cell not in values:
                fills[cell] = "#F0F2F4"
                notes[cell] = "Не установлено при автоматическом разборе; см. исходник и issues.json. Пустое поле не равно нулю и не подтверждает отсутствие оборудования. Точная комплектация не подбиралась."
    start = schedule["last_row"] + 1
    installed = "; ".join(f"{p['system']} — {p['installed_kw'] if p['installed_kw'] is not None else 'не приведено'}" for p in schedule["electric_power"])
    footers = {
        f"A{start}": "Жёлтый — рабочие допущения Д1–Д3. Серый — сведения не установлены при автоматическом разборе; это не ноль и не подтверждение отсутствия.",
        f"A{start+1}": "Д1. В T — потребляемая мощность нагрева. Установочная мощность, кВт: " + (installed or "не приведена") + ". T не переносится автоматически в ведомость электрических нагрузок.",
        f"A{start+2}": "Д2. «бак.сек.» принято как «бактерицидная секция»; модель и параметры неизвестны. AJ совмещает четвёртый фильтр и дополнительную секцию; класс фильтра сохраняется отдельно.",
        f"A{start+3}": "Д3. X — «Мощность расч. (кВт)» охладителя; квалификация «полная/явная» не установлена. Источники и исключения: review.pdf, issues.json.",
    }
    return {"template": str(template), "sheet": schedule["sheet"], "values": values, "notes": notes, "fills": fills,
            "number_formats": {f"K{row}": "0.###" for row in rows}, "footers": footers,
            "headers": {"A1": "ХАРАКТЕРИСТИКА ОТОПИТЕЛЬНО-ВЕНТИЛЯЦИОННОГО ОБОРУДОВАНИЯ — РАБОЧИЕ ДОПУЩЕНИЯ",
                        "G5": "м³/ч", "T3": "Потребл.\nмощность", "X3": "Мощность\nрасч.",
                        "AG2": "Фильтры / обеззараживание*", "AG3": "Ступень / дополнение", "AI4": 3, "AJ4": "4 / доп.*"},
            "last_row": schedule["last_row"], "authored": str(work / "authored.xlsx")}


def render_workbook(schedule: dict, template: Path, output: Path, work: Path, *, preview=False) -> dict:
    paths = runtime_paths()
    if not paths["node"].is_file() or not (paths["modules"] / "@oai/artifact-tool").is_dir():
        raise WorkflowError("document_runtime_unavailable", {"needed": "Node and @oai/artifact-tool", "action": "Use load_workspace_dependencies or KHVS_NODE/KHVS_NODE_MODULES"})
    work.mkdir(parents=True, exist_ok=True)
    script = work / "workbook.mjs"
    shutil.copy2(Path(__file__).with_suffix(".mjs"), script)
    (work / "node_modules").symlink_to(paths["modules"], target_is_directory=True)
    cfg = configuration(schedule, template, work)
    cfg["output"] = str(output)
    if preview:
        preview_end = min(schedule["last_row"], 35)
        cfg["preview"] = {str(work / "left.png"): f"A1:M{preview_end}",
                          str(work / "right.png"): f"N1:AJ{preview_end}",
                          str(work / "notes.png"): f"A{schedule['last_row']+1}:AJ{schedule['last_row']+4}"}
    write_json(work / "workbook-request.json", cfg)
    result = subprocess.run([str(paths["node"]), str(script), str(work / "workbook-request.json")],
                            capture_output=True, text=True, timeout=120)
    (work / "workbook-process.log").write_text(result.stdout + result.stderr)
    if result.returncode:
        raise WorkflowError("workbook_authoring_failed", {"log": str(work / "workbook-process.log")})
    preserve_template(cfg, output)
    checks = verify_workbook(cfg, output)
    if preview:
        # Reimport the final package: an edited imported rich-text cell may render
        # its stale pre-edit text in the authoring instance despite a correct export.
        result = subprocess.run([str(paths["node"]), str(script), str(work / "workbook-request.json"), "preview"],
                                capture_output=True, text=True, timeout=120)
        with (work / "workbook-process.log").open("a") as log:
            log.write(result.stdout + result.stderr)
        if result.returncode:
            raise WorkflowError("workbook_preview_failed", {"log": str(work / "workbook-process.log")})
    return checks


def preserve_template(cfg: dict, output: Path) -> None:
    from lxml import etree as E
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    n = {"m": ns}
    q = lambda tag: "{" + ns + "}" + tag
    with zipfile.ZipFile(cfg["template"]) as z:
        original = {i.filename: (i, z.read(i.filename)) for i in z.infolist()}
    with zipfile.ZipFile(cfg["authored"]) as z:
        authored = {i.filename: z.read(i.filename) for i in z.infolist()}
    sheet = E.fromstring(original["xl/worksheets/sheet1.xml"][1])
    asheet = E.fromstring(authored["xl/worksheets/sheet1.xml"])
    styles = E.fromstring(original["xl/styles.xml"][1])
    astyles = E.fromstring(authored["xl/styles.xml"])
    ocells = {c.get("r"): c for c in sheet.findall(".//m:sheetData/m:row/m:c", n)}
    acells = {c.get("r"): c for c in asheet.findall(".//m:sheetData/m:row/m:c", n)}
    shared = ["".join(si.itertext()) for si in E.fromstring(authored.get("xl/sharedStrings.xml", f'<sst xmlns="{ns}"/>'.encode())).findall("m:si", n)]
    oxfs, axfs = styles.find("m:cellXfs", n), astyles.find("m:cellXfs", n)
    components, formats, xfs = {}, {}, {}

    def col_number(address):
        total = 0
        for letter in re.match(r"[A-Z]+", address)[0]:
            total = total * 26 + ord(letter) - 64
        return total

    def ensure_cell(addr):
        if addr in ocells:
            return
        row_number = int(re.search(r"\d+$", addr)[0])
        data = sheet.find("m:sheetData", n)
        row = next((x for x in data if int(x.get("r")) == row_number), None)
        if row is None:
            row = E.Element(q("row"), r=str(row_number))
            at = next((i for i, x in enumerate(data) if int(x.get("r")) > row_number), len(data))
            data.insert(at, row)
        style = row.get("s") if row.get("customFormat") == "1" else None
        if style is None:
            style = next((x.get("style") for x in sheet.findall("m:cols/m:col", n)
                          if int(x.get("min")) <= col_number(addr) <= int(x.get("max")) and x.get("style")), "0")
        cell = E.Element(q("c"), r=addr, s=style)
        at = next((i for i, x in enumerate(row) if col_number(x.get("r")) > col_number(addr)), len(row))
        row.insert(at, cell)
        ocells[addr] = cell

    for address in set(cfg["values"]) | set(cfg["headers"]) | set(cfg["fills"]) | set(cfg["number_formats"]):
        ensure_cell(address)

    def component(kind, index):
        key = (kind, index)
        if key not in components:
            target = styles.find("m:" + kind, n)
            target.append(deepcopy(astyles.find("m:" + kind, n)[index]))
            target.set("count", str(len(target)))
            components[key] = len(target) - 1
        return components[key]

    def number_format(index):
        if index < 164:
            return index
        if index in formats:
            return formats[index]
        item = next(x for x in astyles.find("m:numFmts", n) if int(x.get("numFmtId")) == index)
        target = styles.find("m:numFmts", n)
        if target is None:
            target = E.Element(q("numFmts"))
            styles.insert(0, target)
        match = next((x for x in target if x.get("formatCode") == item.get("formatCode")), None)
        if match is None:
            match = deepcopy(item)
            match.set("numFmtId", str(max([163] + [int(x.get("numFmtId")) for x in target]) + 1))
            target.append(match)
            target.set("count", str(len(target)))
        formats[index] = int(match.get("numFmtId"))
        return formats[index]

    def style_for(addr, new=False, aid=None):
        aid = int(aid if aid is not None else acells[addr].get("s", "0"))
        oid = int(ocells[addr].get("s", "0")) if not new else None
        fill, fmt = addr in cfg["fills"], addr in cfg["number_formats"]
        key = (new, oid, aid, fill, fmt)
        if key in xfs:
            return xfs[key]
        if new:
            xf = deepcopy(axfs[aid])
            xf.set("xfId", "0")
            for attr, kind in (("fontId", "fonts"), ("fillId", "fills"), ("borderId", "borders")):
                xf.set(attr, str(component(kind, int(xf.get(attr, "0")))))
            xf.set("numFmtId", str(number_format(int(xf.get("numFmtId", "0")))))
        else:
            xf = deepcopy(oxfs[oid])
            if fill:
                xf.set("fillId", str(component("fills", int(axfs[aid].get("fillId", "0")))))
                xf.set("applyFill", "1")
            if fmt:
                xf.set("numFmtId", str(number_format(int(axfs[aid].get("numFmtId", "0")))))
                xf.set("applyNumberFormat", "1")
        oxfs.append(xf)
        oxfs.set("count", str(len(oxfs)))
        xfs[key] = len(oxfs) - 1
        return xfs[key]

    def copy_value(src, dst):
        for child in list(dst):
            if E.QName(child).localname in ("v", "is", "f"):
                dst.remove(child)
        typ = src.get("t", "n")
        dst.attrib.pop("t", None)
        if typ in ("s", "str"):
            value = src.find("m:v", n).text
            dst.set("t", "inlineStr")
            t = E.SubElement(E.SubElement(dst, q("is")), q("t"))
            t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            t.text = shared[int(value)] if typ == "s" else value
        elif typ == "inlineStr":
            dst.set("t", typ)
            dst.append(deepcopy(src.find("m:is", n)))
        elif typ in ("n", "b"):
            if typ == "b":
                dst.set("t", typ)
            value = src.find("m:v", n)
            if value is not None:
                dst.append(deepcopy(value))
        else:
            raise WorkflowError("unsupported_authored_cell_type", {"type": typ})

    for addr in cfg["values"] | cfg["headers"]:
        copy_value(acells[addr], ocells[addr])
    for addr in set(cfg["fills"]) | set(cfg["number_formats"]):
        ocells[addr].set("s", str(style_for(addr)))
    sheet_data = sheet.find("m:sheetData", n)
    for row in list(sheet_data):
        if cfg["last_row"] < int(row.get("r")) <= cfg["last_row"] + 4:
            sheet_data.remove(row)
    for row in asheet.findall("m:sheetData/m:row", n):
        if not cfg["last_row"] < int(row.get("r")) <= cfg["last_row"] + 4:
            continue
        clone = deepcopy(row)
        for cell in clone:
            cell.set("s", str(style_for(cell.get("r"), new=True, aid=cell.get("s", "0"))))
            copy_value(acells[cell.get("r")], cell)
        position = next((i for i, current in enumerate(sheet_data) if int(current.get("r")) > int(clone.get("r"))), len(sheet_data))
        sheet_data.insert(position, clone)
    merges = sheet.find("m:mergeCells", n)
    if merges is None:
        merges = E.Element(q("mergeCells"))
        sheet.insert(list(sheet).index(sheet.find("m:sheetData", n)) + 1, merges)
    for row in range(cfg["last_row"] + 1, cfg["last_row"] + 5):
        E.SubElement(merges, q("mergeCell"), ref=f"A{row}:AJ{row}")
    merges.set("count", str(len(merges)))
    dimension = sheet.find("m:dimension", n)
    old_last = int(re.search(r"\d+$", dimension.get("ref")).group())
    dimension.set("ref", f"A1:AJ{max(old_last,cfg['last_row']+4)}")
    relpath = "xl/worksheets/_rels/sheet1.xml.rels"
    rels = E.fromstring(original[relpath][1]) if relpath in original else E.Element("{http://schemas.openxmlformats.org/package/2006/relationships}Relationships")
    arels = E.fromstring(authored[relpath])
    used = {x.get("Id") for x in rels}
    ridmap, newparts = {}, {}
    for ar in arels:
        if ar.get("Type").rsplit("/", 1)[-1] not in ("comments", "vmlDrawing"):
            continue
        old, newid = ar.get("Id"), ar.get("Id")
        while newid in used:
            newid += "khvs"
        used.add(newid)
        ridmap[old] = newid
        node = deepcopy(ar)
        node.set("Id", newid)
        rels.append(node)
        target = ar.get("Target")
        part = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl/worksheets", target))
        if part in original:
            raise WorkflowError("note_part_collision")
        newparts[part] = authored[part]
    for element in asheet:
        if E.QName(element).localname == "legacyDrawing":
            node = deepcopy(element)
            node.set("{" + rel + "}id", ridmap[node.get("{" + rel + "}id")])
            sheet.append(node)
    types = E.fromstring(original["[Content_Types].xml"][1])
    for element in E.fromstring(authored["[Content_Types].xml"]):
        if E.QName(element).localname == "Override" and element.get("PartName", "").lstrip("/") in newparts:
            types.append(deepcopy(element))
        elif E.QName(element).localname == "Default" and element.get("Extension") == "vml" and not any(x.get("Extension") == "vml" for x in types):
            types.append(deepcopy(element))
    book = E.fromstring(original["xl/workbook.xml"][1])
    for element in book.findall("m:definedNames/m:definedName", n):
        if element.get("name") == "_xlnm.Print_Area":
            escaped = cfg["sheet"].replace("'", "''")
            original_last = re.search(r"\$(\d+)$", element.text or "")
            end = max(int(original_last[1]) if original_last else 0, cfg["last_row"] + 4)
            element.text = f"'{escaped}'!$A$1:$AJ${end}"
    serialize = lambda obj: E.tostring(obj, xml_declaration=True, encoding="UTF-8", standalone=True)
    changes = {"xl/worksheets/sheet1.xml": serialize(sheet), "xl/styles.xml": serialize(styles),
               relpath: serialize(rels), "[Content_Types].xml": serialize(types), "xl/workbook.xml": serialize(book)}
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, (info, raw) in original.items():
            archive.writestr(info, changes.pop(name, raw))
        for name, raw in (changes | newparts).items():
            archive.writestr(name, raw)


def verify_workbook(cfg: dict, output: Path) -> dict:
    import openpyxl
    from copy import copy
    before = openpyxl.load_workbook(cfg["template"]).active
    after = openpyxl.load_workbook(output).active
    from lxml import etree as E
    with zipfile.ZipFile(cfg["template"]) as archive:
        original_tree = E.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    explicit = {e.get("r") for e in original_tree.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c")}
    changes = cfg["values"] | cfg["headers"] | cfg["footers"]
    errors = []
    for addr, value in changes.items():
        if after[addr].value != value:
            errors.append({"cell": addr, "kind": "value"})
    for row in before:
        for cell in row:
            target = after[cell.coordinate]
            if cell.coordinate not in changes and cell.value != target.value:
                errors.append({"cell": cell.coordinate, "kind": "unrelated_value"})
            if cfg["last_row"] < cell.row <= cfg["last_row"] + 4:
                continue
            if cell.coordinate not in explicit:
                continue
            for prop in ("font", "alignment", "border", "protection"):
                if copy(getattr(cell, prop)) != copy(getattr(target, prop)):
                    errors.append({"cell": cell.coordinate, "kind": prop})
            if cell.coordinate not in cfg["fills"] and copy(cell.fill) != copy(target.fill):
                errors.append({"cell": cell.coordinate, "kind": "unrelated_fill"})
            if cell.coordinate not in cfg["number_formats"] and cell.number_format != target.number_format:
                errors.append({"cell": cell.coordinate, "kind": "unrelated_number_format"})
    for addr, note in cfg["notes"].items():
        if not after[addr].comment or after[addr].comment.text != note:
            errors.append({"cell": addr, "kind": "note"})
    for addr, color in cfg["fills"].items():
        if after[addr].fill.fgColor.rgb != "FF" + color.lstrip("#").upper():
            errors.append({"cell": addr, "kind": "fill"})
    if before.page_setup != after.page_setup or before.page_margins != after.page_margins:
        errors.append({"kind": "print_settings"})
    for index, dim in before.row_dimensions.items():
        if index <= cfg["last_row"] and any(getattr(dim, p) != getattr(after.row_dimensions[index], p) for p in ("height", "hidden", "outlineLevel", "collapsed")):
            errors.append({"kind": "row_dimensions", "row": index})
    for index, dim in before.column_dimensions.items():
        if any(getattr(dim, p) != getattr(after.column_dimensions[index], p) for p in ("width", "hidden", "min", "max", "outlineLevel")):
            errors.append({"kind": "column_dimensions", "column": index})
    expected = set(map(str, before.merged_cells.ranges)) | {f"A{r}:AJ{r}" for r in range(cfg["last_row"] + 1, cfg["last_row"] + 5)}
    if set(map(str, after.merged_cells.ranges)) != expected:
        errors.append({"kind": "merges"})
    result = {"passed": not errors, "errors": errors, "values": len(cfg["values"]), "notes": len(cfg["notes"])}
    write_json(output.parent / "workbook-check.json", result)
    if errors:
        raise WorkflowError("workbook_verification_failed", {"errors": errors[:8]})
    return result
