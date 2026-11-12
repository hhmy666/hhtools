"""Integer-cell geometry for 2 by 2 Tile footprints (no I/O)."""


def footprint(point):
    x, y = point
    return frozenset(((x, y), (x + 1, y), (x, y + 1), (x + 1, y + 1)))


def overlap_area(left, right):
    return max(0, 2 - abs(left[0] - right[0])) * max(0, 2 - abs(left[1] - right[1]))


def overlap_profile(point, adjacent_layer):
    return tuple(sorted((area for other in adjacent_layer
                         for area in (overlap_area(point, other),) if area), reverse=True))


def within_bounds(point, board, safe_bounds=(2, 14, 2, 16)):
    x, y = point
    lo_x, hi_x, lo_y, hi_y = safe_bounds
    return lo_x <= x <= hi_x and lo_y <= y <= hi_y and 0 <= x <= board[0]-2 and 0 <= y <= board[1]-2


def symmetry_orbit(point, axes, symmetries=()):
    """Close the actual source symmetry group using frozen min+max axes."""
    points = {point}
    for name in symmetries:
        if name == 'horizontal':
            points |= {(axes[0]-x, y) for x, y in tuple(points)}
        elif name == 'vertical':
            points |= {(x, axes[1]-y) for x, y in tuple(points)}
        elif name == 'rotation':
            points |= {(axes[0]-x, axes[1]-y) for x, y in tuple(points)}
        else:
            raise ValueError('unknown symmetry: '+str(name))
    return tuple(sorted(points))
