"""KN Daily Activity Report layout, based on the supplied Word template."""
import base64
import io
from html import escape
from itertools import zip_longest
from pathlib import Path

from PIL import Image as PILImage, ImageOps
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import (BaseDocTemplate, Table, TableStyle, Paragraph,
                               Spacer, Image, KeepTogether)

PAGE_W, PAGE_H = A4
MARGIN = 8 * mm
WIDTH = PAGE_W - 2 * MARGIN
BLUE = colors.HexColor('#d9e2f3')
LINE = colors.HexColor('#555555')
NORMAL = ParagraphStyle('kn-normal', fontName='Helvetica', fontSize=8, leading=10)
BOLD = ParagraphStyle('kn-bold', parent=NORMAL, fontName='Helvetica-Bold')
HEADER_CENTER = ParagraphStyle('kn-header-center', parent=BOLD, alignment=1)
SMALL = ParagraphStyle('kn-small', parent=NORMAL, fontSize=7, leading=8.5)
PHOTO_CAPTION = ParagraphStyle('kn-photo-caption', parent=SMALL, fontName='Helvetica-Oblique')
DOCUMENTATION_TITLE = ParagraphStyle('documentation-title', parent=BOLD, fontSize=16, leading=19, alignment=1)
DOCUMENTATION_GREEN = colors.HexColor('#d6e3bc')
CENTER = ParagraphStyle('kn-center', parent=SMALL, alignment=1)
WEATHER_ICON_DIR = Path(__file__).parent / 'static' / 'pdf_assets' / 'weather'
WEATHER_ICONS = {
    'sunny': 'sunny', 'clear': 'sunny', 'cerah': 'sunny',
    'cloud': 'cloud', 'cloudy': 'cloud', 'partly cloudy': 'cloud',
    'overcast': 'cloud', 'berawan': 'cloud', 'mendung': 'cloud',
    'rain': 'rain', 'rainy': 'rain', 'light rain': 'rain', 'heavy rain': 'rain',
    'hujan': 'rain', 'hujan ringan': 'rain', 'hujan lebat': 'rain',
    'lightning': 'lightning', 'thunderstorm': 'lightning',
    'petir': 'lightning', 'badai petir': 'lightning',
}


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
            [cell(h, HEADER_CENTER) for h in headings]*2]
    # Empty sections have one placeholder row; no fixed blank row padding.
    for a,b in zip_longest(left or [['-']*n], right or [['-']*n], fillvalue=['']*n):
        rows.append([cell(v) for v in a+b])
    return table(rows, half_widths*2, 2, [('SPAN',(0,0),(n-1,0)),('SPAN',(n,0),(-1,0)),
                 ('VALIGN',(0,1),(-1,1),'MIDDLE'),
                 ('LEFTPADDING',(0,1),(-1,1),2),('RIGHTPADDING',(0,1),(-1,1),2)])


def manpower(people):
    out=[]
    for p in people:
        if not any(str(p.get(k) or '').strip() for k in ('name','role','hours','qty')):
            continue
        label = ' / '.join(str(p[k]).strip() for k in ('name','role') if p.get(k))
        if p.get('_display_scope'):
            label = f"{label or '-'} / {p['_display_scope']}"
        # Each named entry represents one person, unless an explicit quantity is supplied.
        qty = p.get('qty', 1 if p.get('name') else '-')
        out.append([len(out)+1,label or '-',qty,p.get('hours')])
    return out


def weather_cell(value, max_width=12*mm, max_height=8*mm):
    """Keep the supplied label and add the matching original Word icon."""
    label = text(value)
    icon = WEATHER_ICONS.get(' '.join(label.casefold().split()))
    if not icon:
        return cell(label, CENTER)
    path = WEATHER_ICON_DIR / f'{icon}.png'
    with PILImage.open(path) as source:
        scale = min(max_width/source.width, max_height/source.height)
        width, height = source.width*scale, source.height*scale
    return [Image(str(path), width=width, height=height, hAlign='CENTER'),
            Spacer(1, 2), cell(label, CENTER)]


def weather(report):
    legacy=report.get('weather') or {}
    periods=['Morning','Afternoon','Evening','Dusk']
    values=report.get('kn_weather_intervals')
    if values is None:
        values=[legacy.get(k,'') for k in periods]
    values=(list(values)+['']*4)[:4]
    times=['07.00–10.00','10.00–13.00','13.00–16.00','16.00–18.00']
    headings=[cell(f'{name}\n{time}',HEADER_CENTER) for name,time in zip(periods,times)]
    headings += [cell(v,HEADER_CENTER) for v in ['Wind','Temperature (°C)','Impact on Work']]
    rows=[[cell('A. WEATHER REPORT',BOLD)]+['']*6,headings,
          [weather_cell(v,18*mm,13*mm) for v in values]+
          [cell(legacy.get(k),CENTER) for k in ['Wind','Temperature','Impact']]]
    return table(rows,[WIDTH/7]*7,2,[('SPAN',(0,0),(-1,0)),
                 ('ALIGN',(0,1),(-1,-1),'CENTER'),('VALIGN',(0,1),(-1,-1),'MIDDLE')])


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
        empty=table([[cell('DOCUMENTATION PHOTO',DOCUMENTATION_TITLE)], [cell('-')]], [WIDTH],
                    commands=[('BACKGROUND',(0,0),(-1,0),DOCUMENTATION_GREEN)])
        return [empty]
    # A continuous grid fills remaining page space. The image height keeps
    # at most four complete photo rows on an A4 page with the repeated header.
    cw=WIDTH/3;card_w=cw-6;image_h=40*mm
    rows=[[cell('DOCUMENTATION PHOTO',DOCUMENTATION_TITLE),'','']]
    for r in range(0,len(photos),3):
        prepared=[]
        for area,desc,source in photos[r:r+3]:
            try: image=cover_image(source,card_w,image_h)
            except Exception as exc:
                raise ValueError('A documentation photo cannot be read. Replace it before generating.') from exc
            label=' / '.join(str(v).strip() for v in (report.get('photo_documentation_title'),area) if v)
            prepared.append((cell(label,BOLD),cell(desc,PHOTO_CAPTION),image))
        # Align image tops across the row without truncating longer captions.
        title_h=max(title.wrap(card_w-4,PAGE_H)[1] for title,_,_ in prepared)+4
        caption_h=max(caption.wrap(card_w-4,PAGE_H)[1] for _,caption,_ in prepared)+4
        cards=[]
        for title,caption,image in prepared:
            card=Table([[title],[caption],[image]],colWidths=[card_w],
                       rowHeights=[title_h,caption_h,image_h],hAlign='LEFT')
            card.setStyle(TableStyle([
                ('VALIGN',(0,0),(-1,-1),'TOP'),
                ('LEFTPADDING',(0,0),(-1,-1),0),
                ('RIGHTPADDING',(0,0),(-1,-1),0),
                ('TOPPADDING',(0,0),(-1,-1),0),
                ('BOTTOMPADDING',(0,0),(-1,-1),0),
                ('LEFTPADDING',(0,0),(0,1),2),
                ('RIGHTPADDING',(0,0),(0,1),2),
                ('TOPPADDING',(0,0),(0,1),2),
                ('BOTTOMPADDING',(0,0),(0,1),2),
                ('BACKGROUND',(0,0),(0,0),colors.HexColor('#dcecf6')),
                ('BACKGROUND',(0,1),(0,1),colors.HexColor('#f8f9fa')),
                ('BOX',(0,0),(-1,-1),.5,colors.HexColor('#00b7ef')),
                ('LINEBELOW',(0,0),(0,1),.4,colors.HexColor('#00b7ef')),
            ]))
            cards.append(card)
        rows.append(cards+['']*(3-len(cards)))
    grid=table(rows,[cw]*3,1,[('SPAN',(0,0),(-1,0)),
               ('BACKGROUND',(0,0),(-1,0),DOCUMENTATION_GREEN),
               ('LEFTPADDING',(0,1),(-1,-1),3),('RIGHTPADDING',(0,1),(-1,-1),3),
               ('TOPPADDING',(0,1),(-1,-1),3),('BOTTOMPADDING',(0,1),(-1,-1),3)])
    grid.splitInRow=0  # Keep each photo and its caption together.
    return [grid]


def signatures(report, signature_cell):
    entries=report.get('sign_offs') or []
    if not entries:
        entries=[{'label':label,'name':report.get(key)} for label,key in
                 [('Prepared by','prepared_by'),('Checked by','checked_by'),('Approved by','approved_by'),('Approved by PMO','approved_pmo')]]
    result=[]
    for start in range(0,len(entries),4):
        group=entries[start:start+4];w=WIDTH/len(group)
        rows=[[cell(p.get('label'),HEADER_CENTER) for p in group],
              [signature_cell(p.get('_sig_path') or p.get('sig'),w) for p in group],
              [cell(p.get('name'),CENTER) for p in group],
              [cell(p.get('role'),CENTER) for p in group]]
        result.append(KeepTogether([table(rows,[w]*len(group),commands=[
            ('ALIGN',(0,0),(-1,-1),'CENTER'),('VALIGN',(0,0),(-1,-1),'MIDDLE')]),Spacer(1,3*mm)]))
    return result


def progress_table(report):
    import math
    def number(value):
        value=str(value if value is not None else '').strip().replace('%','').replace(' ','')
        value=value.replace(',','.') if ',' in value and '.' not in value else value.replace(',','')
        try:
            value=float(value)
            return value if math.isfinite(value) else None
        except ValueError:
            return None
    def percent(value):
        n=number(value)
        return f'{n:g}%' if n is not None else text(value)
    small=ParagraphStyle('progress-small',parent=CENTER,fontSize=5.5,leading=7)
    bold=ParagraphStyle('progress-bold',parent=small,fontName='Helvetica-Bold')
    base=['description','duration','weight_factor','start','finish']
    metrics=['cumulative_previous_plan','cumulative_previous_actual','this_period_plan',
             'this_period_actual','cumulative_to_date_plan','cumulative_to_date_actual']
    rows=[[cell('H. CONCLUSION',BOLD)]+['']*12,
          [cell(v,bold) for v in ['No.','Description','Duration','Weight Factor','Start','Finish',
           'Cumulative Previous','','This Period','','Cumulative To Date','','']],
          ['']*6+[cell(v,bold) for v in ['Plan','Actual','Plan','Actual','Plan','Actual','Deviation']]]
    entries=[r for r in report.get('overall_progress',[]) if isinstance(r,dict) and any(str(v or '').strip() for v in r.values())]
    totals={k:None for k in metrics}
    for i,r in enumerate(entries,1):
        values=[str(i)]+[percent(r.get(k)) if k=='weight_factor' else r.get(k) for k in base]
        values += [percent(r.get(k)) for k in metrics]
        plan,actual=number(r.get(metrics[-2])),number(r.get(metrics[-1]))
        values += [percent(actual-plan) if plan is not None and actual is not None else '-']
        rows.append([cell(v,small) for v in values])
        weight=number(r.get('weight_factor'))
        for k in metrics:
            value=number(r.get(k))
            if weight is not None and value is not None:
                totals[k]=(totals[k] or 0)+weight*value/100
    commands=[('SPAN',(0,0),(-1,0)),('SPAN',(6,1),(7,1)),('SPAN',(8,1),(9,1)),
              ('SPAN',(10,1),(12,1)),('VALIGN',(0,1),(-1,-1),'MIDDLE'),
              ('LEFTPADDING',(0,1),(-1,-1),2),('RIGHTPADDING',(0,1),(-1,-1),2)]
    commands += [('SPAN',(i,1),(i,2)) for i in range(6)]
    if entries:
        plan,actual=totals[metrics[-2]],totals[metrics[-1]]
        rows.append([cell('OVERALL PROGRESS',bold)]+['']*5+
                    [cell(percent(totals[k]),bold) for k in metrics]+
                    [cell(percent(actual-plan) if plan is not None and actual is not None else '-',bold)])
        commands += [('SPAN',(0,len(rows)-1),(5,len(rows)-1)),('BACKGROUND',(0,len(rows)-1),(-1,len(rows)-1),BLUE)]
    else:
        rows.append([cell('-',small) for _ in range(13)])
    widths=[6,35,12,13,15,15]+[14]*7
    return table(rows,[WIDTH*w/sum(widths) for w in widths],3,commands)


def build_story(report, signature_cell):
    today=named_lines(report,'activities_today'); tomorrow=named_lines(report,'activities_tomorrow')
    indirect=[dict(p, _display_scope='Global') for p in (report.get('indirect_manpower') or [])]
    direct=[]
    for area in report.get('areas',[]):
        indirect.extend(dict(p, _display_scope=str(area.get('id') or '').strip() or 'Area not specified')
                        for p in (area.get('indirect_manpower') or []))
        direct.extend(area.get('manpower') or [])
    story=[weather(report),Spacer(1,4*mm),
           paired('B. ACTIVITY TODAY','C. ACTIVITY TOMMOROW',['No.','Description'],
                  [[i,v] for i,v in enumerate(today,1)],[[i,v] for i,v in enumerate(tomorrow,1)],
                  [8*mm,WIDTH/2-8*mm]),Spacer(1,4*mm),
           paired('D. INDIRECT MANPOWER','E. DIRECT MANPOWER',['No.','Description','Qty','Working Hours'],
                  manpower(indirect),manpower(direct),[7*mm,WIDTH/2-39*mm,10*mm,22*mm]),Spacer(1,4*mm),
           numbered('F. CONSTRAINTS / PROBLEM',named_lines(report,'constraints')),Spacer(1,4*mm),
           numbered('G. REMARKS',lines(report.get('global_remarks'))+named_lines(report,'remarks')),Spacer(1,4*mm)]
    if report.get('show_conclusion', True) not in (False, 'false', 'off', '0', 0):
        if report.get('kn_conclusion_mode') == 'overall_progress':
            story += [progress_table(report), Spacer(1,5*mm)]
        else:
            keys=['description','bq_contract','progress_today','total_accum','progress_percent']
            entries=[r for r in report.get('kn_conclusion',[]) if any(r.get(k) is not None and str(r.get(k)).strip() for k in keys)]
            rows=[[cell('H. CONCLUSION',BOLD)]+['']*5,
                  [cell(h,HEADER_CENTER) for h in ['No.','Description','BQ Contract','Progress Today','Total Accum.','Progress (%)']]]
            rows += [[cell(i)]+[cell(r.get(k)) for k in keys] for i,r in enumerate(entries,1)] or [[cell(1)]+[cell('-')]*5]
            story += [table(rows,[8*mm,WIDTH-96*mm,22*mm,22*mm,22*mm,22*mm],2,[
                ('SPAN',(0,0),(-1,0)),('VALIGN',(0,1),(-1,1),'MIDDLE'),
                ('LEFTPADDING',(0,1),(-1,1),2),('RIGHTPADDING',(0,1),(-1,1),2)]),Spacer(1,5*mm)]
    story += signatures(report,signature_cell)
    story += [Spacer(1,4*mm)]+photo_tables(report)
    return story


def signature_image(source, width):
    if not source:
        return Spacer(1,20*mm)
    if isinstance(source,str) and source.startswith('data:'):
        source=io.BytesIO(base64.b64decode(source.split(',',1)[1]))
    try:
        with PILImage.open(source) as raw:
            image=ImageOps.exif_transpose(raw).convert('RGBA')
            scale=min((width-10*mm)/image.width,20*mm/image.height)
            buf=io.BytesIO();image.save(buf,'PNG');buf.seek(0)
            return Image(buf,width=image.width*scale,height=image.height*scale)
    except (OSError,ValueError) as exc:
        raise ValueError('A signature image cannot be read. Please upload it again.') from exc


def generate_kn_pdf(report, output_path, logos, signature_cell=None):
    signature_cell = signature_cell or signature_image
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
