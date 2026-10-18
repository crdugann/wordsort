#!/usr/bin/env python3
"""Build data/tyctr_scope_sequence.pdf from data/spellings.csv."""

from __future__ import annotations

import csv
from collections import OrderedDict
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

HERE = Path(__file__).resolve().parent
CSV_PATH = HERE / "data" / "spellings.csv"
OUT_PATH = HERE / "data" / "tyctr_scope_sequence.pdf"


def main() -> Path:
    rows: list[dict] = []
    with CSV_PATH.open(encoding="utf-8-sig", newline="") as f:
        rows.extend(csv.DictReader(f))

    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "T",
        parent=styles["Heading1"],
        fontSize=18,
        spaceAfter=6,
        textColor=colors.HexColor("#1a3a2a"),
    )
    sub = ParagraphStyle(
        "S",
        parent=styles["Normal"],
        fontSize=9,
        textColor=colors.HexColor("#4a5c52"),
        spaceAfter=12,
    )
    h2 = ParagraphStyle(
        "H2",
        parent=styles["Heading2"],
        fontSize=12,
        spaceBefore=10,
        spaceAfter=6,
        textColor=colors.HexColor("#1f5c45"),
    )
    body = ParagraphStyle("B", parent=styles["Normal"], fontSize=9, leading=12)
    foot = ParagraphStyle(
        "f",
        parent=body,
        fontSize=8,
        textColor=colors.HexColor("#6a7a72"),
    )

    by_lesson: OrderedDict[int, list[str]] = OrderedDict()
    for r in rows:
        les = int(r["Introduced in Lesson"])
        by_lesson.setdefault(les, []).append(r["Symbol (Spelling)"])
    compact = " &nbsp;·&nbsp; ".join(
        f"<b>L{les}</b>: {', '.join(syms)}" for les, syms in by_lesson.items()
    )

    data = [["Lesson", "Symbol", "Pronounced", "As in", "Voice", "BOB"]]
    for r in rows:
        data.append(
            [
                r["Introduced in Lesson"],
                r["Symbol (Spelling)"],
                f"/{r['Pronounced']}/",
                r["As In"],
                r["Voiced or Whispered"],
                r["BOB Stage"],
            ]
        )

    tbl = Table(
        data,
        colWidths=[
            0.7 * inch,
            0.85 * inch,
            1.35 * inch,
            2.1 * inch,
            1.0 * inch,
            0.55 * inch,
        ],
    )
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f5c45")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("ALIGN", (-1, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c5d5cc")),
                (
                    "ROWBACKGROUNDS",
                    (0, 1),
                    (-1, -1),
                    [colors.white, colors.HexColor("#f2f7f4")],
                ),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )

    doc = SimpleDocTemplate(
        str(OUT_PATH),
        pagesize=letter,
        leftMargin=0.6 * inch,
        rightMargin=0.6 * inch,
        topMargin=0.55 * inch,
        bottomMargin=0.55 * inch,
    )
    story = [
        Paragraph("TYCTR Scope &amp; Sequence", title),
        Paragraph(
            "Grapheme–phoneme correspondences (GPCs) by lesson. "
            "Use while choosing sort words.",
            sub,
        ),
        Paragraph("<b>At a glance</b>", h2),
        Paragraph(compact, body),
        Spacer(1, 10),
        Paragraph("<b>Full GPC table</b>", h2),
        tbl,
        Spacer(1, 14),
        Paragraph("Source: data/spellings.csv · Word Sort Builder TYCTR", foot),
    ]
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    doc.build(story)
    return OUT_PATH


if __name__ == "__main__":
    path = main()
    print(f"Wrote {path} ({path.stat().st_size} bytes)")
