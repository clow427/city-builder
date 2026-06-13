import numpy as np
import laspy

CLASS_RGB = {
    "ground":   (40000, 40000, 40000),
    "road":     (10000, 10000, 10000),
    "sidewalk": (50000, 40000, 25000),
    "grass":    (20000, 45000, 15000),
    "car":      (60000, 60000, 0),
    "other":    (20000, 20000, 60000),
}


def write_colored_las(path, points, classes):
    header = laspy.LasHeader(point_format=3, version="1.2")
    las = laspy.LasData(header)
    las.x, las.y, las.z = points[:, 0], points[:, 1], points[:, 2]
    rgb = np.array([CLASS_RGB.get(c, CLASS_RGB["other"]) for c in classes], dtype=np.uint16)
    las.red, las.green, las.blue = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    las.write(path)
