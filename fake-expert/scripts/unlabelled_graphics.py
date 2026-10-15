"""Conservative candidate grouping; visual regions never imply semantic knowledge."""
from __future__ import annotations


def _covered(box, occupied):
    area=(box[2]-box[0])*(box[3]-box[1])
    return any(max(0,min(box[2],other[2])-max(box[0],other[0]))*
               max(0,min(box[3],other[3])-max(box[1],other[1])) >= area*0.8 for other in occupied)


def candidate_regions(drawings, images, occupied, width, height):
    """Return bounded unexplained image/drawing regions and a count of rejected hints."""
    result=[]; rejected=0
    for row in images:
        box=row['bbox']
        if _covered(box,occupied):continue
        if (box[2]-box[0])*(box[3]-box[1]) > width*height*0.8:
            rejected+=1;continue
        result.append({'bbox':list(box),'kind':'xobject','provenance':row.get('xobject_provenance',{})})
        occupied=[*occupied,box]
    remaining=[list(box) for box in drawings if not _covered(box,occupied)
               and box[2]-box[0]>=2 and box[3]-box[1]>=2]
    while remaining:
        union=remaining.pop(0); count=1
        grew=True
        while grew:
            grew=False
            for box in list(remaining):
                if box[0]<=union[2]+18 and box[2]>=union[0]-18 and box[1]<=union[3]+18 and box[3]>=union[1]-18:
                    union=[min(union[0],box[0]),min(union[1],box[1]),max(union[2],box[2]),max(union[3],box[3])]
                    remaining.remove(box);count+=1;grew=True
        area=(union[2]-union[0])*(union[3]-union[1])
        if not width*height*0.005<=area<=width*height*0.6 or union[3]<height*0.05 or union[1]>height*0.95:
            rejected+=1;continue
        result.append({'bbox':union,'kind':'vector-cluster','provenance':{'drawing_count':count}})
    return result,rejected
