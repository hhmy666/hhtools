"""Pure tower geometry; positions are anchors of complete 2×2 cards.

Editor uses 16/85 grid units per layer. Client LayoutHelper.GetStackShift
uses 2/10. Both complete unfolded rectangles are checked independently.
"""
from collections import Counter
from itertools import product

from .geometry import within_bounds

VECTORS = ((0,-1),(-1,0),(0,1),(1,0))
SHIFTS = (16/85, 0.2)
EDITOR_BODY_HEIGHT = 2 * 203.81146 / 181.77777
EDITOR_PIVOT_Y = .554
CLIENT_BODY_HEIGHT = 2 * 1.8 / 1.48


def rectangle(x,y,count=1,direction=0,shift=0):
    dx,dy=VECTORS[direction]
    end_x=x+dx*shift*(count-1)
    end_y=y+dy*shift*(count-1)
    height,pivot=(EDITOR_BODY_HEIGHT,EDITOR_PIVOT_Y) if shift==SHIFTS[0] else (CLIENT_BODY_HEIGHT,.5) if shift==SHIFTS[1] else (2,.5)
    # Unity's pivot Y points upward; editor coordinates point downward.
    top=1-height*(1-pivot); bottom=1+height*pivot
    return (min(x,end_x),min(y,end_y)+top,max(x,end_x)+2,max(y,end_y)+bottom)


def separated(a,b,gap=0):
    return a[2]+gap<=b[0]+1e-9 or b[2]+gap<=a[0]+1e-9 or a[3]+gap<=b[1]+1e-9 or b[3]+gap<=a[1]+1e-9


def inside(rect,width,height):
    return rect[0]>=-1e-9 and rect[1]>=-1e-9 and rect[2]<=width+1e-9 and rect[3]<=height+1e-9


def symmetry(document,tx=0,ty=0):
    anchors={(s.x,s.y) for s in document.stacks}
    points=Counter((t.x+tx,t.y+ty,t.z) for t in document.tiles if (t.x,t.y) not in anchors)
    if not points:
        points=Counter((t.x+tx,t.y+ty,t.z) for t in document.tiles)
    if not points:
        return ((document.width-2)/2,(document.height-2)/2), ()
    lo_x,hi_x=min(p[0] for p in points),max(p[0] for p in points)
    lo_y,hi_y=min(p[1] for p in points),max(p[1] for p in points)
    axes=(lo_x+hi_x,lo_y+hi_y)
    # Preserve actual layer geometry. A rotation-only formation must not gain
    # an invented left/right reflection. For an asymmetric source keep the
    # established symmetric-new-group contract using its bounding center.
    names=[]
    if Counter((axes[0]-x,y,z) for x,y,z in points.elements())==points:
        names.append('x')
    if lo_y!=hi_y and Counter((x,axes[1]-y,z) for x,y,z in points.elements())==points:
        names.append('y')
    if lo_x!=hi_x and lo_y!=hi_y and Counter((axes[0]-x,axes[1]-y,z) for x,y,z in points.elements())==points:
        names.append('rotation')
    return axes,tuple(names) or ('rotation',)


def _transform(point,axes,name):
    x,y=point
    if name=='x': return axes[0]-x,y
    if name=='y': return x,axes[1]-y
    if name=='rotation': return axes[0]-x,axes[1]-y
    raise ValueError('unknown symmetry: '+str(name))


def linked_orbit(original,moved,axes,names):
    """Apply each group transform to both the old point and its new point."""
    pairs={(original,moved)}
    for name in names:
        pairs|={(_transform(before,axes,name),_transform(after,axes,name))
                for before,after in tuple(pairs)}
    return tuple(sorted(pairs))


def orbit(point,axes,names):
    return tuple(sorted({before for before,after in linked_orbit(point,point,axes,names)}))


def source_points(document,translation):
    anchors={(s.x,s.y) for s in document.stacks}
    tx,ty=translation
    return tuple(sorted({(t.x+tx,t.y+ty) for t in document.tiles if (t.x,t.y) not in anchors}))


def tower_fits(x,y,count,direction,points,width,height,gap=0):
    # The editor grid is wider than the agreed display-safe anchor region.
    # This guard also applies to additions at historical out-of-region anchors.
    if not within_bounds((x,y),(width,height)):
        return False
    for shift in (0,)+SHIFTS:
        rect=rectangle(x,y,count,direction,shift)
        if not inside(rect,width,height) or any(not separated(rect,rectangle(*p,shift=shift),gap) for p in points):
            return False
    return True


def towers_separate(a,b,spacing=0):
    # x,y,count,direction records
    return all(separated(rectangle(*a,shift=shift),rectangle(*b,shift=shift),spacing)
               for shift in (0,)+SHIFTS)


def candidates(document,options,translation):
    points=source_points(document,translation)
    axes,names=symmetry(document,*translation)
    regions=options['regions']
    if points:
        lx,hx=min(x for x,y in points),max(x for x,y in points)
        ly,hy=min(y for x,y in points),max(y for x,y in points)
    else:
        lx=hx=(document.width-2)/2; ly=hy=(document.height-2)/2
    def region(x,y):
        result=[]
        if y+2<=ly: result.append('top')
        if y>=hy+2: result.append('bottom')
        if x+2<=lx: result.append('left')
        if x>=hx+2: result.append('right')
        if lx<=x<=hx and ly<=y<=hy: result.append('center')
        return result
    groups={}
    for x,y in product(range(2,min(14,document.width-2)+1),
                       range(2,min(16,document.height-2)+1)):
        group=orbit((x,y),axes,names)
        if group in groups: continue
        if not all(('auto' in regions or set(region(px,py))&set(regions)) and
                   tower_fits(px,py,1,0,points,document.width,document.height,options['formation_gap'])
                   for px,py in group): continue
        if any(not towers_separate((a[0],a[1],1,0),(b[0],b[1],1,0),options['spacing'])
               for i,a in enumerate(group) for b in group[i+1:]): continue
        distance=sum(abs(x-axes[0]/2)+abs(y-axes[1]/2) for x,y in group)/len(group)
        # Prefer rows above/below main formation; center holes participate equally.
        groups[group]=distance
    reverse=options['spread']=='distributed'
    return sorted(groups,key=lambda group:((-1 if reverse else 1)*groups[group],len(group),group))


def translations(document,options):
    tx,ty=options['translate_x'],options['translate_y']
    first=(tx,ty)
    yield first
    if not options['auto_translate'] or not document.tiles: return
    minx,maxx=min(t.x for t in document.tiles),max(t.x for t in document.tiles)
    miny,maxy=min(t.y for t in document.tiles),max(t.y for t in document.tiles)
    choices=product(range(-minx,document.width-1-maxx),range(-miny,document.height-1-maxy))
    for item in sorted(choices,key=lambda p:(abs(p[0]-tx)+abs(p[1]-ty),p)):
        if item!=first: yield item
