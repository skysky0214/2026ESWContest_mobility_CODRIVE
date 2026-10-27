"""Reject discontinuous lane fragments before drawing or ground projection."""


def continuous_lane(points, max_step=.06, minimum=8):
    groups = [[]]
    for x, y in points:
        current = groups[-1]
        if current and (y <= current[-1][1] or abs(x-current[-1][0]) > max_step):
            groups.append([])
        groups[-1].append((x, y))
    best = max(groups, key=lambda g: g[-1][1]-g[0][1] if g else 0)
    return best if len(best) >= minimum else []
