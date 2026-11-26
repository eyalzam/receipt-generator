import io
import os
import sys
import platform
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, black, white
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

import arabic_reshaper
from bidi.algorithm import get_display


def find_font(names, fallback=None):
    """Search for a font file across common OS locations."""
    search_dirs = []
    if platform.system() == 'Windows':
        windir = os.environ.get('WINDIR', r'C:\Windows')
        search_dirs = [os.path.join(windir, 'Fonts')]
    elif platform.system() == 'Darwin':
        search_dirs = [
            '/Library/Fonts',
            '/System/Library/Fonts',
            '/System/Library/Fonts/Supplemental',
            os.path.expanduser('~/Library/Fonts'),
        ]
    else:
        search_dirs = ['/usr/share/fonts', '/usr/local/share/fonts', os.path.expanduser('~/.fonts')]

    app_dir = os.path.dirname(os.path.abspath(__file__))
    search_dirs.insert(0, os.path.join(app_dir, 'fonts'))

    for d in search_dirs:
        for name in names:
            path = os.path.join(d, name)
            if os.path.exists(path):
                return path
    return fallback


FONT_PATH = find_font(['Arial Unicode.ttf', 'Arial Unicode MS.ttf', 'arialuni.ttf', 'Arial.ttf', 'arial.ttf'])
FONT_PATH_BOLD = find_font(['Arial Bold.ttf', 'arialbd.ttf', 'Arial Unicode.ttf', 'arialuni.ttf', 'Arial.ttf', 'arial.ttf'])

if not FONT_PATH:
    raise RuntimeError("No Hebrew-capable font found. Place arial.ttf or arialuni.ttf in the 'fonts' folder.")

pdfmetrics.registerFont(TTFont('ArialUnicode', FONT_PATH))
pdfmetrics.registerFont(TTFont('ArialBold', FONT_PATH_BOLD or FONT_PATH))

PAGE_W, PAGE_H = A4

COLOR_HEADER_BG = HexColor('#2c3e50')
COLOR_ACCENT = HexColor('#2980b9')
COLOR_LIGHT_BG = HexColor('#f8f9fa')
COLOR_BORDER = HexColor('#dee2e6')
COLOR_TEXT = HexColor('#2c3e50')
COLOR_MUTED = HexColor('#6c757d')
COLOR_SUCCESS = HexColor('#27ae60')


def heb(text):
    """Reshape and reorder Hebrew text for correct RTL display in PDF."""
    if not text:
        return ''
    reshaped = arabic_reshaper.reshape(str(text))
    return get_display(reshaped)


def format_date_hebrew(date_str):
    """Convert YYYY-MM-DD to DD/MM/YYYY."""
    try:
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        return dt.strftime('%d/%m/%Y')
    except (ValueError, TypeError):
        return date_str or ''


def format_amount(amount):
    return f'₪ {amount:,.2f}'


def generate_receipt_pdf(issuer, receipt):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)

    margin_left = 25 * mm
    margin_right = 25 * mm
    content_width = PAGE_W - margin_left - margin_right
    right_x = PAGE_W - margin_right
    left_x = margin_left

    y = PAGE_H - 20 * mm

    receipt_num = receipt['receipt_number']
    receipt_date = format_date_hebrew(receipt['date'])
    amount = receipt['amount']
    notes = receipt.get('notes', '') or ''

    # === HEADER BAR ===
    header_h = 18 * mm
    c.setFillColor(COLOR_HEADER_BG)
    c.rect(0, y - header_h, PAGE_W, header_h, fill=1, stroke=0)

    c.setFillColor(white)
    c.setFont('ArialUnicode', 9)
    c.drawRightString(right_x, y - 7 * mm, heb('מסמך ממוחשב'))

    c.setFont('ArialBold', 16)
    title_text = heb(f'קבלה {receipt_num}')
    c.drawCentredString(PAGE_W / 2, y - 8 * mm, title_text)

    c.setFont('ArialUnicode', 9)
    c.drawCentredString(PAGE_W / 2, y - 14 * mm, heb('(מקור)'))

    seal_path = os.path.join(os.path.dirname(__file__), 'extracted_img_0.png')
    if os.path.exists(seal_path):
        seal_size = 15 * mm
        seal_x = left_x + 2 * mm
        seal_y = y - header_h + (header_h - seal_size) / 2
        c.drawImage(seal_path, seal_x, seal_y, seal_size, seal_size,
                     preserveAspectRatio=True, mask='auto')

    y -= header_h + 8 * mm

    # === ISSUER SECTION ===
    section_h = 30 * mm
    c.setFillColor(COLOR_LIGHT_BG)
    c.setStrokeColor(COLOR_BORDER)
    c.roundRect(left_x, y - section_h, content_width, section_h, 3, fill=1, stroke=1)

    c.setFillColor(COLOR_ACCENT)
    c.setFont('ArialBold', 11)
    c.drawRightString(right_x - 5 * mm, y - 6 * mm, heb('פרטי המנפיק'))

    c.setFillColor(COLOR_TEXT)
    c.setFont('ArialUnicode', 10)
    row_y = y - 12 * mm
    line_h = 5 * mm

    col_right = right_x - 5 * mm
    col_val_right = col_right - 25 * mm

    details = [
        ('שם:', issuer['name']),
        ('ע.פ:', issuer['tax_id']),
        ('כתובת:', issuer['address']),
    ]
    details2 = [
        ('טלפון:', issuer['phone']),
        ('אימייל:', issuer['email']),
        ('', ''),
    ]

    mid_x = left_x + content_width / 2

    for i, ((lbl, val), (lbl2, val2)) in enumerate(zip(details, details2)):
        row = row_y - i * line_h
        c.setFont('ArialBold', 9)
        c.drawRightString(col_right, row, heb(lbl))
        c.setFont('ArialUnicode', 9)
        c.drawRightString(col_val_right, row, heb(val))

        if lbl2:
            c.setFont('ArialBold', 9)
            c.drawRightString(mid_x - 5 * mm, row, heb(lbl2))
            c.setFont('ArialUnicode', 9)
            c.drawRightString(mid_x - 30 * mm, row, heb(val2))

    y -= section_h + 6 * mm

    # === CLIENT ("FOR") SECTION ===
    section_h = 22 * mm
    c.setFillColor(white)
    c.setStrokeColor(COLOR_BORDER)
    c.roundRect(left_x, y - section_h, content_width, section_h, 3, fill=1, stroke=1)

    c.setFillColor(COLOR_ACCENT)
    c.setFont('ArialBold', 11)
    c.drawRightString(col_right, y - 6 * mm, heb('עבור'))

    c.setFillColor(COLOR_TEXT)
    row_y = y - 12 * mm

    client_details_left = [
        ('שם:', receipt['client_name']),
        ('ע.מ/ת.ז:', receipt['client_company_id']),
    ]
    client_details_right = [
        ('טלפון:', receipt['client_phone']),
        ('', ''),
    ]

    for i, ((lbl, val), (lbl2, val2)) in enumerate(zip(client_details_left, client_details_right)):
        row = row_y - i * line_h
        c.setFont('ArialBold', 9)
        c.drawRightString(col_right, row, heb(lbl))
        c.setFont('ArialUnicode', 9)
        c.drawRightString(col_val_right, row, heb(val))

        if lbl2:
            c.setFont('ArialBold', 9)
            c.drawRightString(mid_x - 5 * mm, row, heb(lbl2))
            c.setFont('ArialUnicode', 9)
            c.drawRightString(mid_x - 30 * mm, row, heb(val2))

    y -= section_h + 6 * mm

    # === RECEIPT INFO (Date + Number) ===
    info_h = 10 * mm
    c.setFillColor(COLOR_LIGHT_BG)
    c.setStrokeColor(COLOR_BORDER)
    c.roundRect(left_x, y - info_h, content_width, info_h, 3, fill=1, stroke=1)

    c.setFillColor(COLOR_TEXT)
    c.setFont('ArialBold', 9)
    c.drawRightString(col_right, y - 6.5 * mm, heb('קבלה:'))
    c.setFont('ArialUnicode', 9)
    c.drawRightString(col_val_right, y - 6.5 * mm, str(receipt_num))

    c.setFont('ArialBold', 9)
    c.drawRightString(mid_x - 5 * mm, y - 6.5 * mm, heb('תאריך:'))
    c.setFont('ArialUnicode', 9)
    c.drawRightString(mid_x - 30 * mm, y - 6.5 * mm, receipt_date)

    y -= info_h + 6 * mm

    # === PAYMENT TABLE ===
    table_header_h = 9 * mm
    table_row_h = 9 * mm

    c.setFillColor(COLOR_HEADER_BG)
    c.rect(left_x, y - table_header_h, content_width, table_header_h, fill=1, stroke=0)

    c.setFillColor(white)
    c.setFont('ArialBold', 10)

    col_positions = [
        (right_x - 5 * mm, heb('אמצעי תשלום')),
        (right_x - 55 * mm, heb('פרטים')),
        (right_x - 95 * mm, heb('תאריך')),
        (left_x + 5 * mm, heb('סכום')),
    ]
    for x_pos, text in col_positions:
        if x_pos == left_x + 5 * mm:
            c.drawString(x_pos, y - 6.5 * mm, text)
        else:
            c.drawRightString(x_pos, y - 6.5 * mm, text)

    y -= table_header_h

    # Payment row
    c.setFillColor(white)
    c.setStrokeColor(COLOR_BORDER)
    c.rect(left_x, y - table_row_h, content_width, table_row_h, fill=1, stroke=1)

    c.setFillColor(COLOR_TEXT)
    c.setFont('ArialUnicode', 10)
    c.drawRightString(right_x - 5 * mm, y - 6.5 * mm, heb('העברה בנקאית'))
    c.drawRightString(right_x - 95 * mm, y - 6.5 * mm, receipt_date)
    c.setFont('ArialBold', 10)
    c.drawString(left_x + 5 * mm, y - 6.5 * mm, format_amount(amount))

    y -= table_row_h

    # === TOTALS ===
    totals = [
        (heb('סה״כ'), format_amount(amount), COLOR_TEXT),
        (heb('ניכוי מס במקור'), format_amount(0), COLOR_MUTED),
        (heb('סה"כ לפני ניכוי מס במקור'), format_amount(amount), COLOR_SUCCESS),
    ]

    for label, value, color in totals:
        row_h = 8 * mm
        c.setStrokeColor(COLOR_BORDER)
        c.setFillColor(COLOR_LIGHT_BG if color == COLOR_SUCCESS else white)
        c.rect(left_x, y - row_h, content_width, row_h, fill=1, stroke=1)

        c.setFont('ArialBold', 9)
        c.setFillColor(COLOR_TEXT)
        c.drawRightString(right_x - 5 * mm, y - 5.5 * mm, label)

        c.setFont('ArialBold', 10)
        c.setFillColor(color)
        c.drawString(left_x + 5 * mm, y - 5.5 * mm, value)

        y -= row_h

    y -= 6 * mm

    # === NOTES ===
    if notes:
        c.setFillColor(COLOR_ACCENT)
        c.setFont('ArialBold', 10)
        c.drawRightString(right_x, y, heb('הערות:'))
        y -= 5 * mm

        c.setFillColor(white)
        c.setStrokeColor(COLOR_BORDER)

        note_lines = notes.split('\n')
        notes_h = max(len(note_lines) * 5 * mm + 6 * mm, 12 * mm)
        c.roundRect(left_x, y - notes_h, content_width, notes_h, 3, fill=1, stroke=1)

        c.setFillColor(COLOR_TEXT)
        c.setFont('ArialUnicode', 9)
        note_y = y - 5 * mm
        for line in note_lines:
            if note_y < y - notes_h + 2 * mm:
                break
            c.drawRightString(right_x - 5 * mm, note_y, heb(line))
            note_y -= 4.5 * mm

        y -= notes_h + 6 * mm

    # === SIGNATURE ===
    sig_h = 10 * mm
    c.setFillColor(COLOR_LIGHT_BG)
    c.setStrokeColor(COLOR_BORDER)
    c.roundRect(left_x, y - sig_h, content_width, sig_h, 3, fill=1, stroke=1)

    c.setFillColor(COLOR_TEXT)
    c.setFont('ArialBold', 9)
    c.drawRightString(col_right, y - 6.5 * mm, heb('חתימה:'))
    c.setFont('ArialUnicode', 9)
    c.setFillColor(COLOR_SUCCESS)
    c.drawRightString(col_val_right, y - 6.5 * mm, heb('חתום דיגיטלית'))

    # === FOOTER ===
    footer_y = 15 * mm
    c.setFillColor(COLOR_HEADER_BG)
    c.rect(0, 0, PAGE_W, footer_y + 5 * mm, fill=1, stroke=0)

    c.setFillColor(HexColor('#95a5a6'))
    c.setFont('ArialUnicode', 7)
    c.drawCentredString(PAGE_W / 2, footer_y, heb('דף 1 מתוך 1'))
    c.drawCentredString(PAGE_W / 2, footer_y - 5 * mm, heb('הופק באמצעות מערכת קבלות - זמיר יוגה וטיפולים'))

    c.showPage()
    c.save()
    buf.seek(0)
    return buf
