"""KN Daily Activity Report layout, based on the supplied Word template."""
import base64
import io
from html import escape
from itertools import zip_longest

from PIL import Image as PILImage, ImageOps
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import (BaseDocTemplate, Table, TableStyle, Paragraph,
                               Spacer, PageBreak, Image, KeepTogether)

PAGE_W, PAGE_H = A4
MARGIN = 8 * mm
WIDTH = PAGE_W - 2 * MARGIN
BLUE = colors.HexColor('#d9e2f3')
LINE = colors.HexColor('#555555')
NORMAL = ParagraphStyle('kn-normal', fontName='Helvetica', fontSize=8, leading=10)
BOLD = ParagraphStyle('kn-bold', parent=NORMAL, fontName='Helvetica-Bold')
SMALL = ParagraphStyle('kn-small', parent=NORMAL, fontSize=7, leading=8.5)
CENTER = ParagraphStyle('kn-center', parent=SMALL, alignment=1)


def text(value):
    return '-' if value is None or str(value).strip() == '' else str(value).strip()


def cell(value, style=NORMAL):
    return Paragraph(escape(text(value)).replace('\n', '<br/>'), style)


def table(rows, widths, headers=1, commands=()):
    result = Table(rows, colWidths=widths, repeatRows=headers, hAlign='LEFT', splitInRow=1)
    result.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), .45, LINE),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('BACKGROUND', (0, 0), (-1, headers-1), BLUE),
        *commands,
    ]))
    return result


def lines(value):
    if isinstance(value, list):
        return [str(v).strip() for v in value if v is not None and str(v).strip()]
    return [v.strip() for v in str(value or '').splitlines() if v.strip()]


def named_lines(report, key):
    result = []
    for area in report.get('areas', []):
        for value in lines(area.get(key)):
            result.append(f"{area.get('id')}: {value}" if area.get('id') else value)
    return result


def numbered(title, values):
    rows = [[cell(title, BOLD), '']]
    rows += [[cell(i), cell(v)] for i, v in enumerate(values, 1)] or [[cell(1), cell('-')]]
    return table(rows, [8*mm, WIDTH-8*mm], commands=[('SPAN', (0,0), (-1,0))])


def paired(title_a, title_b, headings, left, right, half_widths):
    n = len(headings)
    rows = [[cell(title_a, BOLD)] + ['']*(n-1) + [cell(title_b, BOLD)] + ['']*(n-1),
            [cell(h, BOLD) for h in headings]*2]
    # Empty sections have one placeholder row; no fixed blank row padding.
    for a,b in zip_longest(left or [['-']*n], right or [['-']*n], fillvalue=['']*n):
        rows.append([cell(v) for v in a+b])
    return table(rows, half_widths*2, 2, [('SPAN',(0,0),(n-1,0)),('SPAN',(n,0),(-1,0))])


def manpower(people):
    out=[]
    for p in people:
        if not any(str(p.get(k) or '').strip() for k in ('name','role','hours','qty')):
            continue
        label = ' / '.join(str(p[k]).strip() for k in ('name','role') if p.get(k))
        # Each named entry represents one person, unless an explicit quantity is supplied.
        qty = p.get('qty', 1 if p.get('name') else '-')
        out.append([len(out)+1,label or '-',qty,p.get('hours')])
    return out


def weather(report):
    values=(list(report.get('kn_weather_hourly') or [])+['']*11)[:11]
    rows=[[cell('A. WEATHER REPORT',BOLD)]+['']*10,
          [cell(f'{h:02d}:00 - {h+1:02d}:00',CENTER) for h in range(7,18)]]
    commands=[('SPAN',(0,0),(-1,0))]
    observed=any(str(v or '').strip() for v in values)
    legacy=[f'{k}: {v}' for k,v in (report.get('weather') or {}).items() if str(v or '').strip()]
    if observed:
        rows.append([cell(v,CENTER) for v in values])
        legacy=[f'{k}: {v}' for k,v in (report.get('weather') or {}).items()
                if k in ('Wind','Temperature','Impact') and str(v or '').strip()]
    if legacy:
        row=len(rows);rows.append([cell('Reported weather: '+ '; '.join(legacy))]+['']*10)
        commands.append(('SPAN',(0,row),(-1,row)))
    elif not observed:
        rows.append([cell('-')]+['']*10);commands.append(('SPAN',(0,2),(-1,2)))
    return table(rows,[WIDTH/11]*11,2,commands)


def header_table(report):
    rows=[]
    for a,ak,b,bk in [('Project No','project_no','Customer','customer'),
                      ('Project Name','project_title','Location / Area','location'),
                      ('Work Package','work_package','Equipment','equipment')]:
        rows.append([cell(a,SMALL),cell(report.get(ak),SMALL),cell(b,SMALL),cell(report.get(bk),SMALL)])
    t=table(rows,[28*mm,WIDTH/2-28*mm,28*mm,WIDTH/2-28*mm],0)
    # No shaded header row for the identity grid.
    t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.white)]))
    return t


def cover_image(source, width, height):
    if isinstance(source,str) and source.startswith('data:'):
        source=io.BytesIO(base64.b64decode(source.split(',',1)[1]))
    with PILImage.open(source) as raw:
        image=ImageOps.exif_transpose(raw).convert('RGB')
        image=ImageOps.fit(image,(round(width*2),round(height*2)),method=PILImage.Resampling.LANCZOS)
        buf=io.BytesIO();image.save(buf,'JPEG',quality=85);buf.seek(0)
    return Image(buf,width=width,height=height)


def photo_tables(report):
    photos=[]
    for area in report.get('areas',[]):
        for photo in area.get('photos',[]):
            source=photo.get('_photo_path') or photo.get('img_data')
            if source:
                photos.append((area.get('id'),photo.get('desc'),source))
            elif photo.get('photo_filename') or photo.get('desc'):
                raise ValueError('A documentation photo is unavailable. Upload it again before generating.')
    if not photos:
        return [numbered('DOCUMENTATION PHOTO',[])]
    result=[];cw=WIDTH/3;image_h=34*mm
    for start in range(0,len(photos),12):
        if result: result.append(PageBreak())
        rows=[[cell('DOCUMENTATION PHOTO',BOLD),'','']]
        for r in range(start,min(start+12,len(photos)),3):
            cards=[]
            for area,desc,source in photos[r:min(r+3,start+12)]:
                try: image=cover_image(source,cw-8,image_h)
                except Exception as exc:
                    raise ValueError('A documentation photo cannot be read. Replace it before generating.') from exc
                label=' / '.join(str(v).strip() for v in (report.get('photo_documentation_title'),area) if v)
                cards.append([cell(label,BOLD),image,cell(desc,SMALL)])
            rows.append(cards+['']*(3-len(cards)))
        grid=table(rows,[cw]*3,1,[('SPAN',(0,0),(-1,0))])
        grid.splitInRow=0  # Keep each photo and its caption together.
        result.append(grid)
    return result


def signatures(report, signature_cell):
    entries=report.get('sign_offs') or []
    if not entries:
        entries=[{'label':label,'name':report.get(key)} for label,key in
                 [('Prepared by','prepared_by'),('Checked by','checked_by'),('Approved by','approved_by'),('Approved by PMO','approved_pmo')]]
    result=[]
    for start in range(0,len(entries),4):
        group=entries[start:start+4];w=WIDTH/len(group)
        rows=[[cell(p.get('label'),BOLD) for p in group],
              [signature_cell(p.get('_sig_path') or p.get('sig'),w) for p in group],
              [cell(p.get('name'),CENTER) for p in group],
              [cell(p.get('role'),CENTER) for p in group]]
        result.append(KeepTogether([table(rows,[w]*len(group)),Spacer(1,3*mm)]))
    return result


def build_story(report, signature_cell):
    today=named_lines(report,'activities_today'); tomorrow=named_lines(report,'activities_tomorrow')
    indirect=list(report.get('indirect_manpower') or [])
    direct=[]
    for area in report.get('areas',[]):
        indirect.extend(area.get('indirect_manpower') or [])
        direct.extend(area.get('manpower') or [])
    story=[weather(report),Spacer(1,4*mm),
           paired('B. ACTIVITY TODAY','C. ACTIVITY TOMMOROW',['No.','Description'],
                  [[i,v] for i,v in enumerate(today,1)],[[i,v] for i,v in enumerate(tomorrow,1)],
                  [8*mm,WIDTH/2-8*mm]),Spacer(1,4*mm),
           paired('D. INDIRECT MANPOWER','E. DIRECT MANPOWER',['No.','Description','Qty','Working Hours'],
                  manpower(indirect),manpower(direct),[7*mm,WIDTH/2-39*mm,10*mm,22*mm]),PageBreak(),
           numbered('F. CONSTRAINTS / PROBLEM',named_lines(report,'constraints')),Spacer(1,4*mm),
           numbered('G. REMARKS',lines(report.get('global_remarks'))+named_lines(report,'remarks')),Spacer(1,4*mm)]
    keys=['description','bq_contract','progress_today','total_accum','progress_percent']
    entries=[r for r in report.get('kn_conclusion',[]) if any(r.get(k) is not None and str(r.get(k)).strip() for k in keys)]
    rows=[[cell('H. CONCLUSION',BOLD)]+['']*5,
          [cell(h,BOLD) for h in ['No.','Description','BQ Contract','Progress Today','Total Accum.','Progress (%)']]]
    rows += [[cell(i)]+[cell(r.get(k)) for k in keys] for i,r in enumerate(entries,1)] or [[cell(1)]+[cell('-')]*5]
    story += [table(rows,[8*mm,WIDTH-96*mm,22*mm,22*mm,22*mm,22*mm],2,[('SPAN',(0,0),(-1,0))]),Spacer(1,5*mm)]
    story += signatures(report,signature_cell)
    story += [PageBreak()]+photo_tables(report)
    return story


def generate_kn_pdf(report, output_path, logos, signature_cell):
    identity=header_table(report)
    _,identity_h=identity.wrap(WIDTH,PAGE_H)
    header_bottom=PAGE_H-35*mm-identity_h
    frame_top=header_bottom-6*mm
    if frame_top < 130*mm:
        raise ValueError('Project identity is too long for the repeated header. Please shorten it.')
    class NumberedCanvas(canvas.Canvas):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs);self.pages=[]
        def showPage(self):
            self.pages.append(dict(self.__dict__));self._startPage()
        def save(self):
            total=len(self.pages)
            for state in self.pages:
                self.__dict__.update(state)
                self.saveState()
                self.setFont('Helvetica',7);self.drawRightString(PAGE_W-MARGIN,5*mm,f'Page {self._pageNumber} of {total}')
                self.restoreState();super().showPage()
            super().save()
    def draw_header(c,doc):
        c.saveState()
        for key,x in [('gpa',MARGIN),('kn',PAGE_W-MARGIN-49*mm)]:
            if logos.get(key):
                c.drawImage(logos[key],x,PAGE_H-18*mm,width=49*mm,height=12*mm,preserveAspectRatio=True,anchor='c',mask='auto')
        c.setFillColor(colors.HexColor('#edf0f7'));c.setStrokeColor(LINE)
        c.rect(MARGIN,PAGE_H-28*mm,WIDTH,8*mm,fill=1,stroke=1)
        c.setFillColor(colors.HexColor('#777777'));c.setFont('Helvetica-Bold',14)
        c.drawCentredString(PAGE_W/2,PAGE_H-25.5*mm,'DAILY ACTIVITY REPORT')
        c.setFont('Helvetica-Bold',9)
        c.drawString(MARGIN,PAGE_H-33*mm,'Date: '+text(report.get('date')))
        c.drawRightString(PAGE_W-MARGIN,PAGE_H-33*mm,'Days: '+text(report.get('day_no')))
        identity.drawOn(c,MARGIN,header_bottom)
        c.restoreState()
    out=io.BytesIO()
    doc=BaseDocTemplate(out,pagesize=A4,leftMargin=MARGIN,rightMargin=MARGIN,
                         topMargin=PAGE_H-frame_top,bottomMargin=10*mm)
    # Zero frame padding makes every body table line up exactly with the header grid.
    from reportlab.platypus import Frame, PageTemplate
    doc.addPageTemplates(PageTemplate(id='kn',frames=[Frame(MARGIN,10*mm,WIDTH,frame_top-10*mm,
        leftPadding=0,rightPadding=0,topPadding=0,bottomPadding=0)],onPage=draw_header))
    doc.build(build_story(report,signature_cell),canvasmaker=NumberedCanvas)
    if output_path:
        from pathlib import Path
        target=Path(output_path);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(out.getvalue())
    out.seek(0);return out
