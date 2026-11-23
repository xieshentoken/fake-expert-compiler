"""Restricted geometric proof of printing strokes outside the complete MediaBox.

Never uses clipping or color to hide marks. Every painted path, including its
conservative transformed stroke envelope, must be strictly outside the paper.
The caller additionally requires an exact-white full-page Poppler render.
"""
from __future__ import annotations
import math

from compiler_version import NATIVE_PRINT_MARK_PROTOCOL as PROTOCOL


def outside_print_marks(page, operations) -> bool:
    def numbers(values, count):
        result = [float(v) for v in values]
        if len(result) != count or not all(math.isfinite(v) for v in result):
            raise ValueError('invalid_print_mark_operands')
        return result
    def object_value(value):
        return value.get_object() if hasattr(value, 'get_object') else value
    resources = object_value(page.get('/Resources', {}))
    box = [float(v) for v in page.mediabox]
    matrix = (1., 0., 0., 1., 0., 0.)
    width, miter, stack, path, strokes, clipping = 1., 10., [], [], 0, False
    def point(x, y):
        a,b,c,d,e,f = matrix
        return (a*x+c*y+e, b*x+d*y+f)
    try:
        for operands, op in operations:
            if op == b'q':
                stack.append((matrix, width, miter))
            elif op == b'Q':
                matrix, width, miter = stack.pop()
            elif op == b'cm':
                a,b,c,d,e,f = numbers(operands, 6)
                A,B,C,D,E,F = matrix
                matrix = (a*A+b*C,a*B+b*D,c*A+d*C,c*B+d*D,e*A+f*C+E,e*B+f*D+F)
                if not all(math.isfinite(v) for v in matrix) or matrix[0]*matrix[3] == matrix[1]*matrix[2]:
                    return False
            elif op == b'w':
                width, = numbers(operands, 1)
                if width <= 0:  # device hairline has no bounded user-space width
                    return False
            elif op == b'M':
                miter, = numbers(operands, 1)
                if miter < 1:
                    return False
            elif op in {b'J', b'j'}:
                value, = numbers(operands, 1)
                if value not in (0,1,2):
                    return False
            elif op == b'd':
                if len(operands) != 2:
                    return False
                dash = [float(v) for v in operands[0]] + [float(operands[1])]
                if not all(math.isfinite(v) and v >= 0 for v in dash):
                    return False
            elif op == b're':
                x,y,w,h = numbers(operands, 4)  # PDF permits negative dimensions
                if not w or not h:
                    return False
                path.extend(point(X,Y) for X,Y in ((x,y),(x+w,y),(x+w,y+h),(x,y+h)))
            elif op in {b'm', b'l'}:
                x,y = numbers(operands, 2)
                if op == b'l' and not path:
                    return False
                path.append(point(x,y))
            elif op == b'h':
                if not path:
                    return False
            elif op in {b'W', b'W*'}:
                if not path or clipping:
                    return False
                clipping = True
            elif op == b'n':
                path, clipping = [], False
            elif op in {b'S', b's'}:
                if not path or clipping:
                    return False
                # Frobenius norm bounds every direction; miter bounds joins and
                # square/round caps. Refuse even tangency with the page edge.
                a,b,c,d,_,_ = matrix
                margin = width * .5 * math.sqrt(a*a+b*b+c*c+d*d) * (math.sqrt(2.) if len(path) == 2 else max(1.,miter))
                xs,ys = zip(*path)
                if not (max(xs)+margin < box[0] or min(xs)-margin > box[2]
                        or max(ys)+margin < box[1] or min(ys)-margin > box[3]):
                    return False
                path, strokes = [], strokes+1
            elif op == b'gs':
                if len(operands) != 1:
                    return False
                state = object_value(object_value(resources.get('/ExtGState', {}))[operands[0]])
                # Stroke adjustment and smoothness affect rasterization only.
                if set(state) - {'/Type','/SA','/SM'} or state.get('/Type', '/ExtGState') != '/ExtGState':
                    return False
                if '/SA' in state and str(state['/SA']) not in ('True','False'):
                    return False
                if '/SM' in state and not 0 <= float(state['/SM']) <= 1:
                    return False
            elif op in {b'CS', b'cs'}:
                if len(operands) != 1:
                    return False
                color = object_value(object_value(resources.get('/ColorSpace', {})).get(operands[0], operands[0]))
                if isinstance(color, str):
                    if color not in ('/DeviceGray','/DeviceRGB','/DeviceCMYK'):
                        return False
                elif len(color) != 4 or str(color[0]) != '/Separation' or str(color[1]) != '/All' or str(color[2]) != '/DeviceGray':
                    return False
            elif op in {b'G',b'g',b'RG',b'rg',b'K',b'k',b'SC',b'SCN',b'sc',b'scn'}:
                if not operands or not all(math.isfinite(float(v)) and 0 <= float(v) <= 1 for v in operands):
                    return False
            else:
                return False
        return strokes > 0 and not stack and not path and not clipping
    except (ValueError,TypeError,KeyError,IndexError,AttributeError,OverflowError):
        return False


def clips_contain_text(page, operations, text_box) -> bool:
    """Every clip must be one axis-aligned rectangle covering all title glyphs."""
    matrix=(1.,0.,0.,1.,0.,0.)
    stack=[]
    rectangle=None
    try:
        for args,op in operations:
            if op==b'q':stack.append(matrix)
            elif op==b'Q':matrix=stack.pop()
            elif op==b'cm':
                a,b,c,d,e,f=map(float,args);A,B,C,D,E,F=matrix
                matrix=(a*A+b*C,a*B+b*D,c*A+d*C,c*B+d*D,e*A+f*C+E,e*B+f*D+F)
            elif op==b're':
                if rectangle is not None:return False
                x,y,w,h=map(float,args);a,b,c,d,e,f=matrix
                if not ((b==c==0) or (a==d==0)):return False
                points=[(a*X+c*Y+e,b*X+d*Y+f) for X,Y in ((x,y),(x+w,y),(x,y+h),(x+w,y+h))]
                xs,ys=zip(*points);rectangle=(min(xs),min(ys),max(xs),max(ys))
            elif op in {b'm',b'l',b'c',b'v',b'y',b'h'}:rectangle=False
            elif op in {b'W',b'W*'}:
                if not rectangle or not (rectangle[0]<=text_box[0] and rectangle[1]<=text_box[1] and rectangle[2]>=text_box[2] and rectangle[3]>=text_box[3]):return False
            elif op in {b'n',b'S',b's',b'f',b'f*',b'F'}:rectangle=None
        return not stack
    except (ValueError,TypeError,IndexError,OverflowError):
        return False
