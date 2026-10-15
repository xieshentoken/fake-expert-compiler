"""P1 crops checked against known pixels and independent full Poppler renders."""
import io
import math
from pathlib import Path
import subprocess

from _support import TempDirTestCase, requires_renderer, pdftoppm
from figure_crop_geometry import pixel_bounds
from visual_semantics import render_crop_png, VisualSemanticsError


@requires_renderer
class FigureCropGeometryTests(TempDirTestCase):
    def make_source(self, rotation=0, offset=False, inset=True):
        from reportlab.pdfgen import canvas
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import RectangleObject
        path=self.tmp/"drawing.pdf"
        c=canvas.Canvas(str(path),pagesize=(600,800))
        c.setFillColorRGB(0,0,0)
        c.rect(200,300,100,100,stroke=0,fill=1)
        c.save()
        page=PdfReader(path).pages[0]
        media=[20,-30,620,770] if offset else [0,0,600,800]
        page.mediabox=RectangleObject(media)
        crop=[100,100,500,700] if inset else media
        page.cropbox=RectangleObject(crop)
        page.rotate(rotation)
        writer=PdfWriter();writer.add_page(page)
        source=self.tmp/"source.pdf"
        with source.open('wb') as f:writer.write(f)
        row={"bbox":[180,370,280,470] if offset else [200,400,300,500],
             "page_width":600,"page_height":800,"physical_page":1,
             "page_geometry":{"media_box":media,"crop_box":crop,"rotation":rotation}}
        return source,row

    def test_black_square_survives_cropbox_offset_rotation_and_dpi(self):
        from PIL import Image, ImageStat
        for offset in (False,True):
            for rotation in (0,90,180,270):
                for dpi in (72,144):
                    with self.subTest(offset=offset,rotation=rotation,dpi=dpi):
                        source,row=self.make_source(rotation,offset)
                        data=render_crop_png(source,row,Path(pdftoppm()),dpi=dpi,padding=0)
                        image=Image.open(io.BytesIO(data)).convert('RGB')
                        self.assertEqual(image.size,(100*dpi//72,100*dpi//72))
                        self.assertEqual(ImageStat.Stat(image).mean,[0,0,0])

    def test_crop_matches_full_render_pixel_for_pixel(self):
        from PIL import Image
        source,row=self.make_source()
        row['bbox']=[199.5,399.5,300.5,500.5]
        prefix=self.tmp/'full'
        subprocess.run([pdftoppm(),'-singlefile','-png','-r','90',str(source),str(prefix)],check=True,capture_output=True)
        full=Image.open(prefix.with_suffix('.png')).convert('RGB')
        expected=full.crop((249,499,376,626))
        actual=Image.open(io.BytesIO(render_crop_png(source,row,Path(pdftoppm()),dpi=90,padding=0))).convert('RGB')
        self.assertEqual(actual.size,expected.size)
        self.assertEqual(actual.tobytes(),expected.tobytes())
        self.assertEqual(render_crop_png(source,row,Path(pdftoppm()),dpi=90,padding=0),
                         render_crop_png(source,row,Path(pdftoppm()),dpi=90,padding=0))

    def test_page_edges_are_clipped_and_invalid_geometry_rejected(self):
        from PIL import Image
        source,row=self.make_source(rotation=90)
        row['bbox']=[0,0,600,800]
        image=Image.open(io.BytesIO(render_crop_png(source,row,Path(pdftoppm()),dpi=72)))
        self.assertEqual(image.size,(800,600))
        for box in ([-1,0,10,10],[0,0,601,10],[10,10,0,0],[0,0,float('nan'),10]):
            with self.subTest(box=box),self.assertRaises(VisualSemanticsError):
                render_crop_png(source,{**row,'bbox':box},Path(pdftoppm()),dpi=72)
        with self.assertRaises(ValueError):
            pixel_bounds({**row,'page_geometry':{**row['page_geometry'],'rotation':45}},72)
