"""Portable review PDF generated from the same data as the workbook."""
from __future__ import annotations

from pathlib import Path
import re
from xml.sax.saxutils import escape

from .extract import WorkflowError, write_json
from .workbook import runtime_paths

LABELS = {"E": "Тип установки", "F": "Бренд по профилю", "G": "Расход воздуха, м³/ч", "J": "Свободное давление, Па",
          "K": "Мощность вентилятора Nуст, кВт", "L": "Напряжение вентилятора, В", "N": "Водяной нагрев: вход, °C",
          "O": "Водяной нагрев: выход, °C", "P": "Водяной нагрев: мощность, кВт", "Q": "Теплоноситель и график, °C",
          "R": "Электронагрев: вход, °C", "S": "Электронагрев: выход, °C", "T": "Электронагрев: потребляемая, кВт",
          "U": "Электронагрев: напряжение, В", "V": "Охлаждение: вход, °C", "W": "Охлаждение: выход, °C",
          "X": "Охлаждение: расчётная мощность, кВт", "Y": "Хладагент", "AG": "Фильтр: ступень 1",
          "AH": "Фильтр: ступень 2", "AI": "Фильтр: ступень 3", "AJ": "4-я ступень / дополнительная секция"}


def value_text(value) -> str:
    return format(value, ".12g").replace(".", ",") if isinstance(value, (int, float)) else str(value)


def cell_key(address):
    col = 0
    for char in re.match(r"[A-Z]+", address)[0]:
        col = col * 26 + ord(char) - 64
    return int(re.search(r"\d+$", address)[0]), col


def render_review(schedule: dict, source: Path, output: Path, work: Path) -> dict:
    from reportlab.pdfgen import canvas
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph
    from reportlab.lib.styles import ParagraphStyle
    from pypdf import PdfReader, PdfWriter
    from pypdf.annotations import Link
    from pypdf.generic import Fit

    fonts = runtime_paths()["modules"] / "pdfjs-dist/standard_fonts"
    if not (fonts / "LiberationSans-Regular.ttf").exists():
        raise WorkflowError("document_runtime_unavailable", {"needed": "LiberationSans fonts from bundled PDF runtime"})
    pdfmetrics.registerFont(TTFont("Khvs", str(fonts / "LiberationSans-Regular.ttf")))
    pdfmetrics.registerFont(TTFont("KhvsBold", str(fonts / "LiberationSans-Bold.ttf")))
    width, height = A4
    margin, usable = 36, width - 72
    ink, blue, grey = colors.HexColor("#172634"), colors.HexColor("#006e9d"), colors.HexColor("#d8dfe5")
    style = ParagraphStyle("body", fontName="Khvs", fontSize=9, leading=12, textColor=ink)
    small = ParagraphStyle("small", parent=style, fontSize=8, leading=10)
    pdf = canvas.Canvas(str(work / "review-front.pdf"), pagesize=A4)
    links, bookmarks = [], {}
    powers = {p["cell"]: p for p in schedule["electric_power"]}

    def paragraph(text, x, y, w, fmt=style):
        p = Paragraph(escape(str(text)).replace("\n", "<br/>"), fmt)
        _, h = p.wrap(w, 2000)
        p.drawOn(pdf, x, y - h)
        return h

    def heading(title, subtitle):
        pdf.setFont("KhvsBold", 18)
        pdf.setFillColor(ink)
        pdf.drawString(margin, height - 45, title)
        paragraph(subtitle, margin, height - 58, usable, small)
        pdf.setStrokeColor(blue)
        pdf.line(margin, height - 86, width - margin, height - 86)
        return height - 104

    def footer():
        pdf.setStrokeColor(grey)
        pdf.line(margin, 32, width - margin, 32)
        pdf.setFont("Khvs", 8)
        pdf.setFillColor(ink)
        pdf.drawString(margin, 20, "Рабочие допущения · Не является инженерной приёмкой")
        pdf.drawRightString(width - margin, 20, str(pdf.getPageNumber()))

    def end_page():
        footer()
        pdf.showPage()

    y = heading("ХОВС / проверка NED", f"{len(schedule['systems'])} систем · {len(schedule['updates'])} заполненных значений · профиль {schedule['profile']}")
    y -= paragraph("Синяя ссылка у значения открывает исходную страницу внутри этого PDF. Для возврата используйте закладки систем. Координаты и полные основания сохранены в values.json.", margin, y, usable) + 15
    y -= paragraph("Жёлтый — рабочее допущение о смысле поля. В T записана потребляемая мощность нагрева; установочная показана отдельно. Неизвестная комплектация не превращается в ноль или отметку отсутствия.", margin, y, usable) + 15
    if schedule["issues"]:
        y -= paragraph(f"Нужна дополнительная проверка: {len(schedule['issues'])} замечаний. Их полный список — issues.json. Результат не следует считать завершённой сверкой.", margin, y, usable) + 15
    for s in schedule["systems"]:
        if y < 90:
            end_page()
            y = heading("Системы / продолжение", "Перейдите к нужной системе по ссылке.")
        pdf.setFillColor(blue)
        pdf.setFont("KhvsBold", 10)
        pdf.drawString(margin + 4, y, s["system"])
        pdf.setFont("Khvs", 9)
        pdf.drawString(margin + 90, y, "Строки Excel: " + ", ".join(map(str, s["rows"])))
        links.append({"from": pdf.getPageNumber() - 1, "rect": [margin, y - 5, width - margin, y + 12], "kind": "system", "target": s["system"]})
        pdf.setStrokeColor(grey)
        pdf.line(margin, y - 10, width - margin, y - 10)
        y -= 27
    end_page()
    assumption_page = pdf.getPageNumber() - 1
    y = heading("Рабочие допущения", "Числа взяты из подбора; смысл общих заголовков обозначен явно.")
    for ident, a in schedule["assumptions"].items():
        pdf.setFont("KhvsBold", 11)
        pdf.setFillColor(ink)
        pdf.drawString(margin, y, f"{ident} · {a['title']}")
        y -= 19
        y -= paragraph(a["decision"], margin, y, usable) + 8
        y -= paragraph(a["limit"], margin, y, usable, small) + 10
        for url in a["urls"]:
            pdf.setFillColor(blue)
            pdf.setFont("Khvs", 8)
            pdf.drawString(margin, y, url)
            pdf.linkURL(url, (margin, y - 3, width - margin, y + 10), relative=0)
            y -= 15
        y -= 14
    paragraph("ККБ, парогенератор и резервный двигатель не заполняются автоматически этим профилем. Если они описаны в исходнике, нужен отдельный разбор секций. Каталог не устанавливает комплектацию данного проекта.", margin, y, usable)
    end_page()

    def table_header(y):
        pdf.setFillColor(colors.HexColor("#edf2f5"))
        pdf.rect(margin, y - 22, usable, 22, fill=1, stroke=0)
        pdf.setFillColor(ink)
        pdf.setFont("KhvsBold", 8)
        for x, text in zip((margin + 5, margin + 48, margin + 232, margin + 443), ("Ячейка", "Параметр", "Значение", "Источник")):
            pdf.drawString(x, y - 14, text)
        return y - 24

    for system in schedule["systems"]:
        bookmarks[system["system"]] = pdf.getPageNumber() - 1
        subtitle = f"Excel: {schedule['sheet']}, строки " + ", ".join(map(str, system["rows"]))
        y = table_header(heading("Проверка: " + system["system"], subtitle))
        updates = sorted((u for u in schedule["updates"] if cell_key(u["cell"])[0] in system["rows"]), key=lambda u: cell_key(u["cell"]))
        for u in updates:
            col = re.match(r"[A-Z]+", u["cell"])[0]
            label = LABELS.get(col, col)
            if len(system["rows"]) == 2 and col in ("G", "J", "K", "L", "AG", "AH"):
                label = ("Приток: " if cell_key(u["cell"])[0] == system["rows"][0] else "Вытяжка: ") + label
            text = value_text(u["value"])
            if u.get("assumption_id"):
                text += " [" + u["assumption_id"] + "]"
            if u["cell"] in powers:
                installed = powers[u["cell"]]["installed_kw"]
                text += "\nУстановочная: " + (value_text(installed) + " кВт" if installed is not None else "не приведена")
            paragraphs = [Paragraph(escape(t).replace("\n", "<br/>"), small) for t in (u["cell"], label, text)]
            widths = (33, 174, 201)
            heights = [p.wrap(w, 2000)[1] for p, w in zip(paragraphs, widths)]
            row_height = max(20, max(heights) + 10)
            if row_height > height - 200:
                raise WorkflowError("review_value_too_long", {"cell": u["cell"]})
            if y - row_height < 125:
                end_page()
                y = table_header(heading("Проверка: " + system["system"] + " / продолжение", subtitle))
            if u.get("assumption_id"):
                pdf.setFillColor(colors.HexColor("#fff0d0"))
                pdf.rect(margin, y - row_height, usable, row_height, fill=1, stroke=0)
            for p, h, x in zip(paragraphs, heights, (margin + 5, margin + 48, margin + 232)):
                p.drawOn(pdf, x, y - 5 - h)
            target = f"PDF, с. {u['page']}"
            pdf.setFont("Khvs", 8)
            pdf.setFillColor(blue)
            pdf.drawString(margin + 443, y - 13, target)
            links.append({"from": pdf.getPageNumber() - 1, "rect": [margin + 438, y - 18, width - margin, y], "kind": "source", "target": u["page"]})
            pdf.setStrokeColor(grey)
            pdf.line(margin, y - row_height, width - margin, y - row_height)
            y -= row_height
        missing = [g["cells"][0] for g in schedule["unfilled"] if cell_key(g["cells"][0])[0] in system["rows"]]
        note = "Не извлечено однозначно: " + ", ".join(missing) + "." if missing else "Все предусмотренные значения извлечены."
        y -= paragraph(note, margin, y - 12, usable, small) + 16
        paragraph("Д1–Д3 — рабочие допущения. Неприведённое значение не равно нулю; подробные замечания — issues.json.", margin, y, usable, small)
        pdf.setFont("Khvs", 8)
        pdf.setFillColor(blue)
        pdf.drawRightString(width - margin, 44, "К списку систем")
        links.append({"from": pdf.getPageNumber() - 1, "rect": [width - margin - 110, 40, width - margin, 55], "kind": "cover", "target": 0})
        end_page()
    pdf.save()

    front, original = PdfReader(work / "review-front.pdf"), PdfReader(source)
    writer = PdfWriter()
    writer.append(front)
    offset = len(writer.pages)
    writer.append(original, import_outline=False)
    writer.add_outline_item("Начало", 0)
    writer.add_outline_item("Допущения", assumption_page)
    for name, page in bookmarks.items():
        writer.add_outline_item("Проверка " + name, page)
    source_outline = writer.add_outline_item("Исходные страницы", offset)
    for system in schedule["systems"]:
        if system["pages"]:
            writer.add_outline_item(system["system"], offset + system["pages"][0] - 1, parent=source_outline)
    for link in links:
        target = offset + link["target"] - 1 if link["kind"] == "source" else bookmarks[link["target"]] if link["kind"] == "system" else 0
        if not 0 <= target < len(writer.pages):
            raise WorkflowError("invalid_evidence_page")
        link["resolved_target"] = target
        writer.add_annotation(link["from"], Link(rect=link["rect"], target_page_index=target, fit=Fit(fit_type="/FitH", fit_args=(float(writer.pages[target].mediabox.top),))))
    writer.add_metadata({"/Title": "ХОВС — источники и рабочие допущения", "/Subject": schedule["profile"]})
    with output.open("wb") as stream:
        writer.write(stream)
    result = PdfReader(output)
    ids = {p.indirect_reference.idnum: i for i, p in enumerate(result.pages)}
    for link in links:
        found = []
        for reference in result.pages[link["from"]].get("/Annots", []):
            annotation = reference.get_object()
            if "/Dest" in annotation and all(abs(float(a) - b) < .1 for a, b in zip(annotation["/Rect"], link["rect"])):
                destination = annotation["/Dest"][0]
                found.append(ids[destination.idnum] if hasattr(destination, "idnum") else int(destination))
        if found != [link["resolved_target"]]:
            raise WorkflowError("pdf_link_verification_failed")
    def page_bytes(page):
        content = page.get_contents()
        return content.get_data() if content is not None else b""

    if any(page_bytes(result.pages[offset + i]) != page_bytes(original.pages[i]) for i in range(len(original.pages))):
        raise WorkflowError("pdf_source_changed")
    check = {"passed": True, "review_pages": offset, "source_pages": len(original.pages), "links": len(links),
             "value_links": len([l for l in links if l["kind"] == "source"]), "source_content_preserved": True}
    write_json(output.parent / "pdf-check.json", check)
    return check
