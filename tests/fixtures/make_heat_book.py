"""Regenerate tests/fixtures/heat-book.pdf (requires PyMuPDF; not needed to run tests).

Five pages: definitions + numbered equation, a raster plot whose values exist
only as pixels, a vector diagram + ruled table, a Form-XObject figure (the
LaTeX ``\\includegraphics{fig.pdf}`` case), and a Chinese section.

Usage: python3 make_heat_book.py OUTPUT.pdf
"""
import sys

import pymupdf

out = sys.argv[1]
doc = pymupdf.open()
W, H = 595, 842


def para(page, y, text, size=10.5):
    rect = pymupdf.Rect(60, y, W - 60, y + 400)
    page.insert_textbox(rect, text, fontsize=size, fontname="helv")


# Page 1: definitions + equation
p = doc.new_page(width=W, height=H)
p.insert_text((60, 70), "Chapter 1 Heat Conduction", fontsize=18, fontname="hebo")
para(p, 100, (
    "1.1 Fourier's Law\n\n"
    "Thermal conductivity k is the material property that relates the conductive heat flux "
    "to the local temperature gradient. Fourier's law states that the heat flux q is "
    "proportional to the negative temperature gradient:\n\n"
    "        q = -k dT/dx                                   (1.1)\n\n"
    "where q is the heat flux in W/m2, k is the thermal conductivity in W/(m K), and dT/dx "
    "is the temperature gradient in K/m. Equation (1.1) is valid for isotropic, homogeneous "
    "media at steady state. It fails for very short time scales (below the phonon relaxation "
    "time) and for rarefied gases where the mean free path is comparable to the geometry. "
    "The thermal diffusivity alpha = k/(rho cp) controls transient response.\n\n"
    "For a plane wall of thickness L with surface temperatures T1 and T2, integration of "
    "(1.1) gives the conduction rate Q = k A (T1 - T2) / L, which defines the conductive "
    "thermal resistance R = L/(k A)."
))

# Page 2: raster plot; the measured values only exist inside the image pixels
p = doc.new_page(width=W, height=H)
p.insert_text((60, 70), "1.2 Temperature Dependence of Conductivity", fontsize=14, fontname="hebo")
para(p, 90, (
    "Figure 1.1 shows the measured thermal conductivity of copper versus temperature. "
    "The peak value and the temperature at which it occurs should be read from the plot."
))
src = pymupdf.open()
sp = src.new_page(width=400, height=260)
sp.draw_rect(pymupdf.Rect(0, 0, 400, 260), color=None, fill=(1, 1, 1))
sp.draw_line((50, 220), (380, 220)); sp.draw_line((50, 220), (50, 20))
pts = [(60, 200), (90, 60), (110, 40), (140, 90), (200, 150), (300, 170), (370, 175)]
for a, b in zip(pts, pts[1:]):
    sp.draw_line(a, b, color=(0.8, 0, 0), width=2)
sp.insert_text((110, 35), "peak 52 W/(m K) at 18 K", fontsize=9)
sp.insert_text((180, 245), "Temperature T (K)", fontsize=9)
sp.insert_text((5, 120), "k", fontsize=9)
pix = sp.get_pixmap(dpi=110)  # raster: text inside becomes pixels
p.insert_image(pymupdf.Rect(80, 140, 480, 400), pixmap=pix)
p.insert_text((80, 420), "Figure 1.1: Thermal conductivity of copper versus temperature.", fontsize=9.5)
para(p, 450, (
    "Above the peak, phonon-phonon (Umklapp) scattering reduces conductivity as temperature "
    "increases. Below the peak, boundary scattering limits the mean free path."
))

# Page 3: vector diagram + table
p = doc.new_page(width=W, height=H)
p.insert_text((60, 70), "1.3 Composite Walls", fontsize=14, fontname="hebo")
para(p, 90, (
    "Figure 1.2 shows the series thermal-resistance network of a three-layer composite wall. "
    "The total resistance is the sum of the layer resistances R1 + R2 + R3."
))
x0 = 100
for i, (lbl, col) in enumerate([("A", (0.9, 0.7, 0.5)), ("B", (0.7, 0.8, 0.9)), ("C", (0.8, 0.9, 0.7))]):
    p.draw_rect(pymupdf.Rect(x0 + 60 * i, 140, x0 + 60 * (i + 1), 300), color=(0, 0, 0), fill=col)
    p.insert_text((x0 + 60 * i + 25, 225), lbl, fontsize=11)
p.draw_line((340, 220), (380, 220))
for i in range(3):
    xs = 380 + 45 * i
    p.draw_polyline([(xs, 220), (xs + 7, 212), (xs + 15, 228), (xs + 22, 212), (xs + 30, 228), (xs + 37, 212), (xs + 45, 220)])
p.insert_text((100, 320), "Figure 1.2: Composite wall and equivalent series resistance network.", fontsize=9.5)
p.insert_text((60, 370), "Table 1.1: Thermal conductivity of common materials at 300 K", fontsize=10, fontname="hebo")
rows = [("Material", "k [W/(m K)]"), ("Copper", "401"), ("Aluminum", "237"), ("Glass", "1.4"), ("Air", "0.026")]
for r, (a, b) in enumerate(rows):
    y = 390 + 20 * r
    p.draw_rect(pymupdf.Rect(60, y, 260, y + 20)); p.draw_rect(pymupdf.Rect(260, y, 400, y + 20))
    p.insert_text((66, y + 14), a, fontsize=10); p.insert_text((266, y + 14), b, fontsize=10)

# Page 4: Form XObject figure (like LaTeX \includegraphics of a PDF figure)
p = doc.new_page(width=W, height=H)
p.insert_text((60, 70), "1.4 Fins", fontsize=14, fontname="hebo")
para(p, 90, (
    "Figure 1.3 compares fin efficiency for straight rectangular fins as a function of the "
    "fin parameter mL. Efficiency falls below 0.5 when mL exceeds roughly 2."
))
fig = pymupdf.open()
fp = fig.new_page(width=300, height=200)
fp.draw_line((30, 170), (290, 170)); fp.draw_line((30, 170), (30, 10))
curve = [(30, 20), (80, 35), (130, 70), (180, 105), (230, 125), (280, 138)]
fp.draw_polyline(curve, color=(0, 0, 0.8), width=1.5)
fp.insert_text((140, 190), "mL", fontsize=8); fp.insert_text((2, 90), "eta_f", fontsize=8)
p.show_pdf_page(pymupdf.Rect(120, 140, 420, 340), fig, 0)
p.insert_text((120, 360), "Figure 1.3: Fin efficiency of a straight rectangular fin.", fontsize=9.5)
para(p, 390, "The fin efficiency is eta_f = tanh(mL)/(mL)    (1.2), with m = sqrt(hP/(kAc)).")

# Page 5: Chinese text
p = doc.new_page(width=W, height=H)
p.insert_font(fontname="cjk", fontbuffer=pymupdf.Font("china-s").buffer)
p.insert_textbox(pymupdf.Rect(60, 60, W - 60, 500), (
    "1.5 对流换热\n\n"
    "对流换热系数h描述流体与固体表面之间的换热强度。牛顿冷却公式为 q = h (Ts - Tf)，"
    "其中Ts为表面温度，Tf为流体温度。对流换热系数取决于流速、流体物性以及几何形状，"
    "自然对流的典型值为2到25 W/(m2 K)，强制对流可达到数百。图1.4给出了平板边界层的速度分布。"
), fontsize=11, fontname="cjk")

doc.save(out)
print(out, doc.page_count)
