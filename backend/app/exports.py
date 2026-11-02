"""Exports render from the SAVED run snapshot, so a file never disagrees with the screen."""
import csv
import io
from xml.sax.saxutils import escape  # nosec B406 - only escapes text, never parses XML

from openpyxl import Workbook
from pptx import Presentation
from pptx.util import Inches, Pt
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle, PageBreak
from reportlab.graphics.shapes import Drawing, String
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.charts.piecharts import Pie


_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value):
    """Neutralise spreadsheet formula injection in text cells (OWASP CSV injection)."""
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value


def fact_table(fact: dict) -> tuple[list[str], list[list]]:
    label_names = list(fact["rows"][0]["labels"]) if fact["rows"] else []
    has_share = any("share_pct" in r for r in fact["rows"])
    header = label_names + ["value"] + (["share_pct"] if has_share else [])
    body = []
    for r in fact["rows"]:
        row = [r["labels"].get(n) for n in label_names] + [r["value"]]
        if has_share:
            row.append(r.get("share_pct"))
        body.append(row)
    return header, body


def to_csv(run: dict) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["fact_id", "description", "group", "value", "share_pct"])
    for f in run["facts"]:
        for r in f["rows"]:
            group = " / ".join(str(v) for v in r["labels"].values())
            w.writerow([safe_cell(x) for x in
                        [f["fact_id"], f["description"], group, r["value"], r.get("share_pct")]])
    return buf.getvalue().encode("utf-8-sig")


def to_xlsx(run: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append(["Question", safe_cell(run["question"])])
    ws.append(["Status", run["status"]])
    ws.append([])
    ws.append(["Finding", "Evidence"])
    for c in run["claims"]:
        ws.append([safe_cell(c["text"]), ", ".join(c["fact_ids"])])
    for f in run["facts"]:
        sheet = wb.create_sheet(f["fact_id"])
        header, body = fact_table(f)
        sheet.append([safe_cell(h) for h in header])
        for row in body:
            sheet.append([safe_cell(x) for x in row])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def fact_chart(fact: dict):
    rows = fact.get("rows", [])[:12]
    if not rows or fact.get("chart") == "kpi": return None
    labels=[" / ".join(str(v) for v in r.get("labels",{}).values()) or "Total" for r in rows]
    values=[float(r.get("value") or 0) for r in rows]
    d=Drawing(500,240)
    if fact.get("chart") == "pie":
        ch=Pie(); ch.x=170; ch.y=20; ch.width=160; ch.height=160; ch.data=values; ch.labels=labels
        d.add(ch)
    elif fact.get("chart") == "line":
        ch=LinePlot(); ch.x=50; ch.y=40; ch.width=400; ch.height=150; ch.data=[list(enumerate(values))]
        ch.xValueAxis.valueMin=0; ch.xValueAxis.valueMax=max(1,len(values)-1); ch.yValueAxis.valueMin=min(0,min(values)); ch.yValueAxis.valueMax=max(values) if max(values)>0 else 1
        d.add(ch)
        for i,l in enumerate(labels[:8]): d.add(String(50+i*(400/max(1,len(labels)-1)),20,l[:12],fontSize=6,textAnchor="middle"))
    else:
        ch=VerticalBarChart(); ch.x=45; ch.y=40; ch.width=410; ch.height=160; ch.data=[values]; ch.categoryAxis.categoryNames=[l[:12] for l in labels]; ch.categoryAxis.labels.angle=30; ch.categoryAxis.labels.fontSize=6; ch.valueAxis.valueMin=0; ch.valueAxis.valueMax=max(values)*1.1 if max(values)>0 else 1
        d.add(ch)
    return d

def to_pdf(run: dict) -> bytes:
    styles = getSampleStyleSheet()
    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, title="InsightFlow report",
                            leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36)
    story = [Paragraph("InsightFlow analysis report", styles["Title"]),
             Paragraph(f"<b>Question:</b> {escape(run['question'])}", styles["Normal"]),
             Paragraph(f"Run {escape(run['id'])} | status: {escape(run['status'])} | "
                       f"created {escape(str(run['created_at']))}", styles["Italic"]),
             Spacer(1, 12), Paragraph("Findings", styles["Heading2"])]
    for c in run["claims"]:
        story.append(Paragraph(f"{escape(c['text'])} <font size=8 color='#667085'>"
                               f"[{escape(', '.join(c['fact_ids']))}]</font>", styles["BodyText"]))
    for f in run["facts"]:
        header, body = fact_table(f)
        story += [Spacer(1, 10), Paragraph(f"{escape(f['fact_id'])}: {escape(f['description'])}", styles["Heading3"])]
        chart = fact_chart(f)
        if chart: story += [chart, Spacer(1, 6)]
        data = [header] + [["" if x is None else (f"{x:,.2f}" if isinstance(x, float) else str(x))
                            for x in row] for row in body[:30]]
        table = Table(data, repeatRows=1)
        table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111827")),
                                   ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                                   ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                                   ("FONTSIZE", (0, 0), (-1, -1), 8)]))
        story.append(table)
    if run["warnings"]:
        story += [Spacer(1, 10), Paragraph("Warnings", styles["Heading3"])]
        story += [Paragraph(escape(w), styles["BodyText"]) for w in run["warnings"]]
    doc.build(story)
    return out.getvalue()


def to_pptx(run: dict) -> bytes:
    """Presentation-ready export generated from the same saved analysis snapshot."""
    prs=Presentation(); prs.slide_width=Inches(13.333); prs.slide_height=Inches(7.5)
    def title(slide, text, subtitle=None):
        tb=slide.shapes.add_textbox(Inches(.6),Inches(.35),Inches(12),Inches(.7)).text_frame
        tb.text=text; tb.paragraphs[0].font.size=Pt(28); tb.paragraphs[0].font.bold=True
        if subtitle:
            p=tb.add_paragraph(); p.text=subtitle; p.font.size=Pt(12)
    slide=prs.slides.add_slide(prs.slide_layouts[6]); title(slide,'InsightFlow Analysis Report',run.get('question',''))
    tf=slide.shapes.add_textbox(Inches(.7),Inches(1.6),Inches(11.8),Inches(4.8)).text_frame
    tf.text='Executive Summary'; tf.paragraphs[0].font.size=Pt(22)
    for c in run.get('claims',[])[:8]:
        p=tf.add_paragraph(); p.text=c.get('text',''); p.level=0; p.font.size=Pt(15)
    for f in run.get('facts',[]):
        slide=prs.slides.add_slide(prs.slide_layouts[6]); title(slide,f.get('description','Analysis'))
        rows=f.get('rows',[])[:12]
        if f.get('chart')=='kpi':
            v=rows[0].get('value') if rows else None
            box=slide.shapes.add_textbox(Inches(.8),Inches(1.6),Inches(11),Inches(2)).text_frame; box.text=str(v if v is not None else 'n/a'); box.paragraphs[0].font.size=Pt(48); box.paragraphs[0].font.bold=True
        else:
            table=slide.shapes.add_table(len(rows)+1,2,Inches(.8),Inches(1.5),Inches(11.5),Inches(5.3)).table
            table.cell(0,0).text='Group'; table.cell(0,1).text='Value'
            for i,r in enumerate(rows,1):
                table.cell(i,0).text=' / '.join(str(x) for x in r.get('labels',{}).values()) or 'Total'; table.cell(i,1).text=str(r.get('value',''))
        # Add an evidence note on every slide.
        note=slide.shapes.add_textbox(Inches(.8),Inches(6.95),Inches(11.5),Inches(.3)).text_frame; note.text=f"Evidence: {f.get('fact_id','')} | chart type: {f.get('chart','table')}"; note.paragraphs[0].font.size=Pt(9)
    out=io.BytesIO(); prs.save(out); return out.getvalue()
