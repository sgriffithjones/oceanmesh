import errno
import logging
import os
from pathlib import Path

import fiona
import geopandas as gpd
import matplotlib.path as mpltPath
import matplotlib.pyplot as plt
import numpy as np
import numpy.linalg
import rasterio
import rasterio.crs
import rasterio.warp
import shapely.geometry
import shapely.validation
from pyproj import CRS
from rasterio.windows import from_bounds

from .grid import Grid
from .region import Region, to_lat_lon

nan = np.nan
fiona_version = fiona.__version__

logger = logging.getLogger(__name__)

__all__ = ["Shoreline", "DEM", "get_polygon_coordinates", "create_circle_coords"]


def create_circle_coords(radius, center, arc_res):
    """
    Given a radius and a center point, creates a numpy array of coordinates
    defining a circle in a CCW direction with a given arc resolution.

    Parameters:
    radius (float): the radius of the circle
    center (tuple): the (x,y) coordinates of the center point
    arc_res (float): the arc resolution of the circle in degrees

    Returns:
    numpy.ndarray: an array of (x,y) coordinates defining the circle
    """
    # Define the angle array with the given arc resolution
    angles = np.arange(0, 360 + arc_res, arc_res) * np.pi / 180

    # Calculate the (x,y) coordinates of the circle points
    x_coords = center[0] + radius * np.cos(angles)
    y_coords = center[1] + radius * np.sin(angles)

    # Combine the (x,y) coordinates into a single array
    coords = np.column_stack((x_coords, y_coords))

    return coords


def get_polygon_coordinates(vector_file):
    """Get the coordinates of a polygon from a vector file or plain csv file"""
    # detect if file is a shapefile or a geojson or geopackage
    if (
        vector_file.endswith(".shp")
        or vector_file.endswith(".geojson")
        or vector_file.endswith(".gpkg")
    ):
        gdf = gpd.read_file(vector_file)
        polygon = np.array(gdf.iloc[0].geometry.exterior.coords.xy).T
    elif vector_file.endswith(".csv"):
        polygon = np.loadtxt(vector_file, delimiter=",")
    return polygon


def _convert_to_array(lst):
    """Converts a list of numpy arrays to a np array"""
    return np.concatenate(lst, axis=0)


def _convert_to_list(arr):
    """Converts a nan-delimited numpy array to a list of numpy arrays"""
    a = np.insert(arr, 0, [[nan, nan]], axis=0)
    tmp = [a[s] for s in np.ma.clump_unmasked(np.ma.masked_invalid(a[:, 0]))]
    return [np.append(a, [[nan, nan]], axis=0) for a in tmp]


def _create_boubox(bbox):
    """Create a bounding box from domain extents `bbox`. Path orientation will be CCW."""
    if isinstance(bbox, tuple):
        xmin, xmax, ymin, ymax = bbox
        return [
            [xmin, ymin],
            [xmax, ymin],
            [xmax, ymax],
            [xmin, ymax],
            [xmin, ymin],
        ]
    return bbox


def _create_ranges(start, stop, N, endpoint=True):
    """Vectorized alternative to numpy.linspace
    https://stackoverflow.com/questions/40624409/vectorized-np-linspace-for-multiple-start-and-stop-values
    """
    if endpoint == 1:
        divisor = N - 1
    else:
        divisor = N
    steps = (1.0 / divisor) * (stop - start)
    return steps[:, None] * np.arange(N) + start[:, None]


def _densify(poly, maxdiff, bbox, radius=0):
    """Fills in any gaps in latitude or longitude arrays
    that are greater than a `maxdiff` (degrees) apart
    """
    logger.debug("Entering:_densify")

    boubox = _create_boubox(bbox)
    path = mpltPath.Path(boubox, closed=True)
    inside = path.contains_points(poly, radius=0.1)  # add a small radius
    lon, lat = poly[:, 0], poly[:, 1]
    nx = len(lon)
    dlat = np.abs(lat[1:] - lat[:-1])
    dlon = np.abs(lon[1:] - lon[:-1])
    nin = np.ceil(np.maximum(dlat, dlon) / maxdiff) - 1
    nin[~inside[1:]] = 0  # no need to densify outside of bbox please
    # handle negative values
    nin[nin < 0] = 0
    sumnin = np.nansum(nin)
    if sumnin == 0:
        return np.hstack((lon[:, None], lat[:, None]))
    nout = sumnin + nx

    lonout = np.full((int(nout)), nan, dtype=float)
    latout = np.full((int(nout)), nan, dtype=float)

    n = 0
    for i in range(nx - 1):
        ni = nin[i]
        if ni == 0 or np.isnan(ni):
            latout[n] = lat[i]
            lonout[n] = lon[i]
            nstep = 1
        else:
            ni = int(ni)
            icoords = _create_ranges(
                np.array([lat[i], lon[i]]),
                np.array([lat[i + 1], lon[i + 1]]),
                ni + 2,
            )
            latout[n : n + ni + 1] = icoords[0, : ni + 1]
            lonout[n : n + ni + 1] = icoords[1, : ni + 1]
            nstep = ni + 1
        n += nstep

    latout[-1] = lat[-1]
    lonout[-1] = lon[-1]

    logger.debug("Exiting:_densify")

    return np.hstack((lonout[:, None], latout[:, None]))


def _poly_area(x, y):
    """Calculates area of a polygon"""
    return 0.5 * np.abs(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1)))


def _poly_length(coords):
    """Calculates circumference of a polygon"""
    if all(np.isclose(coords[0, :], coords[-1, :])):
        c = coords
    else:
        c = np.vstack((coords, coords[0, :]))

    return np.sum(np.sqrt(np.sum(np.diff(c, axis=0) ** 2, axis=1)))


def _classify_shoreline(bbox, boubox, polys, h0, minimum_area_mult, stereo=False):
    """Classify segments in numpy.array `polys` as either `inner` or `mainland`.
    (1) The `mainland` category contains segments that are not totally enclosed inside the `bbox`.
    (2) The `inner` (i.e., islands) category contains segments totally enclosed inside the `bbox`.
        NB: Removes `inner` geometry with area < `minimum_area_mult`*`h0`**2
    (3) `boubox` polygon array is will be clipped by segments contained by `mainland`.
    """
    logger.debug("Entering:_classify_shoreline")

    _AREAMIN = minimum_area_mult * h0**2

    if len(boubox) == 0:
        boubox = _create_boubox(bbox)
        boubox = np.asarray(boubox)
    elif not _is_path_ccw(boubox):
        boubox = np.flipud(boubox)

    boubox = _densify(boubox, h0 / 2, bbox, radius=0.1)

    # Remove nan's (append again at end)
    isNaN = np.sum(np.isnan(boubox), axis=1) > 0
    if any(isNaN):
        boubox = np.delete(boubox, isNaN, axis=0)
    del isNaN

    inner = np.empty(shape=(0, 2))
    inner[:] = nan
    mainland = np.empty(shape=(0, 2))
    mainland[:] = nan

    polyL = _convert_to_list(polys)
    bSGP = shapely.geometry.Polygon(boubox)

    for poly in polyL:
        pSGP = shapely.geometry.Polygon(poly[:-2, :])
        if bSGP.contains(pSGP):
            if stereo:
                # convert back to Lat/Lon coordinates for the area testing
                area = _poly_area(*to_lat_lon(*np.asarray(pSGP.exterior.xy)))
            else:
                area = pSGP.area
            if area >= _AREAMIN:
                inner = np.append(inner, poly, axis=0)
        elif pSGP.overlaps(bSGP):
            if stereo:
                bSGP = pSGP
            else:
                bSGP = bSGP.difference(pSGP)
                # Append polygon segment to mainland
                mainland = np.vstack((mainland, poly))
                # Clip polygon segment from boubox and regenerate path

    out = np.empty(shape=(0, 2))

    if bSGP.geom_type == "Polygon":
        # Convert to `MultiPolygon`
        bSGP = shapely.geometry.MultiPolygon([bSGP])

    # MultiPolygon members can be accessed via iterator protocol using `in`.
    for b in bSGP.geoms:
        xy = np.asarray(b.exterior.coords)
        xy = np.vstack((xy, xy[0]))
        out = np.vstack((out, xy, [nan, nan]))

    logger.debug("Exiting:classify_shoreline")

    return inner, mainland, out


def _chaikins_corner_cutting(coords, refinements=5):
    """http://www.cs.unc.edu/~dm/UNC/COMP258/LECTURES/Chaikins-Algorithm.pdf"""
    logger.debug("Entering:_chaikins_corner_cutting")
    coords = np.array(coords)

    for _ in range(refinements):
        L = coords.repeat(2, axis=0)
        R = np.empty_like(L)
        R[0] = L[0]
        R[2::2] = L[1:-1:2]
        R[1:-1:2] = L[2::2]
        R[-1] = L[-1]
        coords = L * 0.75 + R * 0.25

    logger.debug("Exiting:_chaikins_corner_cutting")
    return coords


def _smooth_shoreline(polys, N):
    """Smoothes the shoreline segment-by-segment using
    a `N` refinement Chaikins Corner cutting algorithm.
    """
    logger.debug("Entering:_smooth_shoreline")

    polys = _convert_to_list(polys)
    out = []
    for poly in polys:
        tmp = _chaikins_corner_cutting(poly[:-1], refinements=N)
        tmp = np.append(tmp, [[nan, nan]], axis=0)
        out.append(tmp)

    logger.debug("Exiting:_smooth_shoreline")

    return _convert_to_array(out)


def _clip_polys_2(polys, bbox, delta=0.10):
    """Clip segments in `polys` that intersect with `bbox`.
    Clipped segments need to extend outside `bbox` to avoid
    false positive `all(inside)` cases. Solution here is to
    add a small offset `delta` to the `bbox`.
    """
    logger.debug("Entering:_clip_polys_2")

    # Inflate bounding box to allow clipped segment to overshoot original box.
    bbox = (bbox[0] - delta, bbox[1] + delta, bbox[2] - delta, bbox[3] + delta)
    boubox = np.asarray(_create_boubox(bbox))
    path = mpltPath.Path(boubox)
    polys = _convert_to_list(polys)

    out = []

    for poly in polys:
        p = poly[:-1, :]

        inside = path.contains_points(p)

        iRemove = []

        _keepLL = True
        _keepUL = True
        _keepLR = True
        _keepUR = True

        if all(inside):
            out.append(poly)
        elif any(inside):
            for j in range(0, len(p)):
                if not inside[j]:  # snap point to inflated domain bounding box
                    px = p[j, 0]
                    py = p[j, 1]
                    if not (bbox[0] < px and px < bbox[1]) or not (
                        bbox[2] < py and py < bbox[3]
                    ):
                        if (
                            _keepLL and px < bbox[0] and py < bbox[2]
                        ):  # is over lower-left
                            p[j, :] = [bbox[0], bbox[2]]
                            _keepLL = False
                        elif (
                            _keepUL and px < bbox[0] and bbox[3] < py
                        ):  # is over upper-left
                            p[j, :] = [bbox[0], bbox[3]]
                            _keepUL = False
                        elif (
                            _keepLR and bbox[1] < px and py < bbox[2]
                        ):  # is over lower-right
                            p[j, :] = [bbox[1], bbox[2]]
                            _keepLR = False
                        elif (
                            _keepUR and bbox[1] < px and bbox[3] < py
                        ):  # is over upper-right
                            p[j, :] = [bbox[1], bbox[3]]
                            _keepUR = False
                        else:
                            iRemove.append(j)

            logger.info(f"Simplify polygon: length {len(p)} --> {len(p)}")
            # Remove colinear||duplicate vertices
            if len(iRemove) > 0:
                p = np.delete(p, iRemove, axis=0)
                del iRemove

            line = p

            # Close polygon
            if not all(np.isclose(line[0, :], line[-1, :])):
                line = np.append(line, [line[0, :], [nan, nan]], axis=0)
            else:
                line = np.append(line, [[nan, nan]], axis=0)

            out.append(line)

    logger.debug("Exiting:_clip_polys_2")

    return _convert_to_array(out)


def _clip_polys(polys, bbox, delta=0.10):
    """Clip segments in `polys` that intersect with `bbox`.
    Clipped segments need to extend outside `bbox` to avoid
    false positive `all(inside)` cases. Solution here is to
    add a small offset `delta` to the `bbox`.
    Dependencies: shapely.geometry and numpy
    """

    logger.debug("Entering:_clip_polys")

    # Inflate bounding box to allow clipped segment to overshoot original box.
    bbox = (bbox[0] - delta, bbox[1] + delta, bbox[2] - delta, bbox[3] + delta)
    boubox = np.asarray(_create_boubox(bbox))
    polyL = _convert_to_list(polys)

    out = np.empty(shape=(0, 2))

    b = shapely.geometry.Polygon(boubox)

    for poly in polyL:
        mp = shapely.geometry.Polygon(poly[:-2, :])
        if not mp.is_valid:
            logger.warning(
                "Shapely.geometry.Polygon "
                + f"{shapely.validation.explain_validity(mp)}."
                + " Applying tiny buffer to make valid."
            )
            mp = mp.buffer(1.0e-6)  # ~0.1m
            if mp.geom_type == "Polygon":
                mp = shapely.geometry.MultiPolygon([mp])
        else:
            mp = shapely.geometry.MultiPolygon([mp])

        for p in mp.geoms:
            pi = p.intersection(b)
            if b.contains(p):
                out = np.vstack((out, poly))
            elif not pi.is_empty:
                # assert(pi.geom_type,'MultiPolygon')
                if pi.geom_type == "Polygon":
                    pi = shapely.geometry.MultiPolygon([pi])

                for ppi in pi.geoms:
                    xy = np.asarray(ppi.exterior.coords)
                    xy = np.vstack((xy, xy[0]))
                    out = np.vstack((out, xy, [nan, nan]))

                del (ppi, xy)
            del pi
        del (p, mp)

    logger.debug("Exiting:_clip_polys")

    return out


def _nth_simplify(polys, bbox):
    """Collapse segments in `polys` outside of `bbox`"""
    logger.debug("Entering:_nth_simplify")

    boubox = np.asarray(_create_boubox(bbox))
    path = mpltPath.Path(boubox)
    polys = _convert_to_list(polys)
    out = []
    for poly in polys:
        j = 0
        inside = path.contains_points(poly[:-2, :])
        line = np.empty(shape=(0, 2))
        while j < len(poly[:-2]):
            if inside[j]:  # keep point (in domain)
                line = np.append(line, [poly[j, :]], axis=0)
            else:  # pt is outside of domain
                bd = min(
                    j + 50, len(inside) - 1
                )  # collapses 50 pts to 1 vertex (arbitary)
                exte = min(50, bd - j)
                if sum(inside[j:bd]) == 0:  # next points are all outside
                    line = np.append(line, [poly[j, :]], axis=0)
                    line = np.append(line, [poly[j + exte, :]], axis=0)
                    j += exte
                else:  # otherwise keep
                    line = np.append(line, [poly[j, :]], axis=0)
            j += 1
        line = np.append(line, [[nan, nan]], axis=0)
        out.append(line)

    logger.debug("Exiting:_nth_simplify")
    return _convert_to_array(out)


def _is_path_ccw(_p):
    """Compute curve orientation from first two line segment of a polygon.
    Source: https://en.wikipedia.org/wiki/Curve_orientation
    """
    detO = 0.0
    O3 = np.ones((3, 3))

    i = 0
    while (i + 3 < _p.shape[0]) and np.isclose(detO, 0.0):
        # Colinear vectors detected. Try again with next 3 indices.
        O3[:, 1:] = _p[i : (i + 3), :]
        detO = np.linalg.det(O3)
        i += 1

    if np.isclose(detO, 0.0):
        raise RuntimeError("Cannot determine orientation from colinear path.")

    return detO > 0.0


def _is_overlapping(bbox1, bbox2):
    """Determines if two axis-aligned boxes intersect"""
    x1min, x1max, y1min, y1max = bbox1
    x2min, x2max, y2min, y2max = bbox2
    return x1min < x2max and x2min < x1max and y1min < y2max and y2min < y1max


def remove_dup(arr: np.ndarray):
    """Remove duplicate element from np.ndarray"""
    result = np.concatenate((arr[np.nonzero(np.diff(arr))[0]], [arr[-1]]))

    return result


class Shoreline(Region):
    """
    The shoreline class extends :class:`Region` to store data
    that is later used to create signed distance functions to
    represent irregular shoreline geometries. This data
    is also involved in developing mesh sizing functions.

    Parameters
    ----------
    shp : str or pathlib.Path
        Path to shapefile containing shoreline data.
    bbox : tuple
        Bounding box of the region of interest. The format is
        (xmin, xmax, ymin, ymax).
    h0 : float
        Minimum grid spacing.
    crs : str, optional
        Coordinate reference system of the shapefile. Default is
        'EPSG:4326'.
    refinements : int, optional
        Number of refinements to apply to the shoreline. Default is 1.
    minimum_area_mult : float, optional
        Minimum area multiplier. Default is 4.0.
        Note that features with area less than h0*minimum_area_mult
        are removed.
    smooth_shoreline : bool, optional
        Smooth the shoreline. Default is True.
    """

    def __init__(
        self,
        shp,
        bbox,
        h0,
        crs="EPSG:4326",
        refinements=1,
        minimum_area_mult=4.0,
        smooth_shoreline=True,
        stereo=False,
    ):
        if isinstance(shp, str):
            shp = Path(shp)

        if isinstance(bbox, tuple):
            _boubox = np.asarray(_create_boubox(bbox))
        else:
            _boubox = np.asarray(bbox)
            if not _is_path_ccw(_boubox):
                _boubox = np.flipud(_boubox)
            bbox = (
                np.nanmin(_boubox[:, 0]),
                np.nanmax(_boubox[:, 0]),
                np.nanmin(_boubox[:, 1]),
                np.nanmax(_boubox[:, 1]),
            )

        super().__init__(bbox, crs)

        self.shp = shp
        self.h0 = h0
        self.inner = []
        self.outer = []
        self.mainland = []
        self.boubox = _boubox
        self.refinements = refinements
        self.minimum_area_mult = minimum_area_mult

        polys = self._read()

        if stereo:
            self.bbox = (
                np.nanmin(polys[:, 0] * 0.99),
                np.nanmax(polys[:, 0] * 0.99),
                np.nanmin(polys[:, 1] * 0.99),
                np.nanmax(polys[:, 1] * 0.99),
            )  # so that bbox overlaps with antarctica > and becomes the outer boundary
            self.boubox = np.asarray(_create_boubox(self.bbox))

        if smooth_shoreline:
            polys = _smooth_shoreline(polys, self.refinements)

        polys = _densify(polys, self.h0, self.bbox)

        polys = _clip_polys(polys, self.bbox)

        self.inner, self.mainland, self.boubox = _classify_shoreline(
            self.bbox, self.boubox, polys, self.h0 / 2, self.minimum_area_mult, stereo
        )

    @property
    def shp(self):
        return self.__shp

    @shp.setter
    def shp(self, filename):
        if not os.path.isfile(filename):
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), filename)
        self.__shp = filename

    @property
    def refinements(self):
        return self.__refinements

    @refinements.setter
    def refinements(self, value):
        if value < 0:
            raise ValueError("Refinements must be > 0")
        self.__refinements = value

    @property
    def minimum_area_mult(self):
        return self.__minimum_area_mult

    @minimum_area_mult.setter
    def minimum_area_mult(self, value):
        if value <= 0.0:
            raise ValueError(
                "Minimum area multiplier * h0**2 to "
                " prune inner geometry must be > 0.0"
            )
        self.__minimum_area_mult = value

    @property
    def h0(self):
        return self.__h0

    @h0.setter
    def h0(self, value):
        if value <= 0:
            raise ValueError("h0 must be > 0")
        self.__h0 = value

    @staticmethod
    def transform_to(gdf, dst_crs):
        """Transform geodataframe ``gdf`` representing
        a shoreline to dst_crs
        """
        dst_crs = CRS.from_user_input(dst_crs)
        if not gdf.crs.equals(dst_crs):
            logger.info(f"Reprojecting shoreline from {gdf.crs} to {dst_crs}")
            gdf = gdf.to_crs(dst_crs)
        return gdf

    def _read(self):
        """Reads a ESRI Shapefile from `filename` ∩ `bbox`"""
        if not isinstance(self.bbox, tuple):
            _bbox = (
                np.amin(self.bbox[:, 0]),
                np.amax(self.bbox[:, 0]),
                np.amin(self.bbox[:, 1]),
                np.amax(self.bbox[:, 1]),
            )
        else:
            _bbox = self.bbox

        logger.debug("Entering: _read")

        msg = f"Reading in ESRI Shapefile {self.shp}"
        logger.info(msg)

        # transform if necessary
        s = self.transform_to(gpd.read_file(self.shp), self.crs)

        # Explode to remove multipolygons or multi-linestrings (if present)
        s = s.explode(index_parts=True)

        polys = []  # store polygons

        delimiter = np.empty((1, 2))
        delimiter[:] = np.nan
        re = numpy.array([0, 2, 1, 3], dtype=int)

        for g in s.geometry:
            # extent of geometry
            bbox2 = [g.bounds[r] for r in re]
            if _is_overlapping(_bbox, bbox2):
                if g.geom_type == "LineString":
                    poly = np.asarray(g.coords)
                elif g.geom_type == "Polygon":  # a polygon
                    poly = np.asarray(g.exterior.coords.xy).T
                else:
                    raise ValueError(f"Unsupported geometry type: {g.geom_type}")

                poly = remove_dup(poly)
                polys.append(np.row_stack((poly, delimiter)))

        if len(polys) == 0:
            raise ValueError("Shoreline data does not intersect with bbox")

        logger.debug("Exiting: _read")

        return _convert_to_array(polys)

    def plot(
        self,
        ax=None,
        xlabel=None,
        ylabel=None,
        title=None,
        file_name=None,
        show=True,
        xlim=None,
        ylim=None,
    ):
        """Visualize the content in the shp field of Shoreline"""
        flg1, flg2 = False, False

        if ax is None:
            fig, ax = plt.subplots()
            ax.axis("equal")

        if len(self.mainland) != 0:
            (line1,) = ax.plot(self.mainland[:, 0], self.mainland[:, 1], "k-")
            flg1 = True
        if len(self.inner) != 0:
            (line2,) = ax.plot(self.inner[:, 0], self.inner[:, 1], "r-")
            flg2 = True
        (line3,) = ax.plot(self.boubox[:, 0], self.boubox[:, 1], "g--")

        xmin, xmax, ymin, ymax = self.bbox
        rect = plt.Rectangle(
            (xmin, ymin),
            xmax - xmin,
            ymax - ymin,
            fill=None,
            hatch="////",
            alpha=0.2,
        )

        border = 0.10 * (xmax - xmin)
        if ax is None:
            plt.xlim(xmin - border, xmax + border)
            plt.ylim(ymin - border, ymax + border)

        ax.add_patch(rect)

        if flg1 and flg2:
            ax.legend((line1, line2, line3), ("mainland", "inner", "outer"))
        elif flg1 and not flg2:
            ax.legend((line1, line3), ("mainland", "outer"))
        elif flg2 and not flg1:
            ax.legend((line2, line3), ("inner", "outer"))

        if xlabel is not None:
            ax.set_xlabel(xlabel)
        if ylabel is not None:
            ax.set_ylabel(ylabel)
        if title is not None:
            ax.set_title(title)

        ax.set_aspect("equal", adjustable="box")

        if show:
            plt.show()
        if file_name is not None:
            plt.savefig(file_name)
        return ax


class DEM(Grid):
    """
    Digitial elevation model read in from a tif or NetCDF file
    """

    def __init__(self, dem, crs="EPSG:4326", bbox=None, extrapolate=False):
        """Read in a DEM from a tif or NetCDF file for later use
        in developing mesh sizing functions.

        Parameters
        ----------
        dem : str or pathlib.Path
            Path to the DEM file
        crs : str, optional
            Coordinate reference system of the DEM, by default 'EPSG:4326'
        bbox : oceanmesh.Region class
            Bounding box of the DEM, by default None.
            Note that if none, it will read in the entire DEM.
        extrapolate : bool, optional
            Extrapolate the DEM outside the bounding box, by default False
        """

        if isinstance(dem, str):
            dem = Path(dem)

        if bbox is not None:
            assert isinstance(bbox, Region), "bbox must be a Region class object"
            # Extract the total bounds from the extent
            bbox = bbox.total_bounds

        if dem.exists():
            msg = f"Reading in {dem}"
            logger.info(msg)
            # Open the raster file using rasterio
            with rasterio.open(dem) as src:
                nodata_value = src.nodata
                self.meta = src.meta
                # entire DEM is read in
                if bbox is None:
                    bbox = src.bounds
                    topobathy = src.read(1)
                # then clip the DEM to the box
                else:
                    #
                    _bbox = (bbox[0], bbox[2], bbox[1], bbox[3])
                    window = from_bounds(*_bbox, transform=src.transform)
                    topobathy = src.read(1, window=window, masked=True)
                    topobathy = np.transpose(topobathy, (1, 0))
            # Ensure its a floating point array
            topobathy = topobathy.astype(np.float64)
            topobathy[
                topobathy == nodata_value
            ] = np.nan  # set the no-data value to nan
        elif not dem.exists():
            raise FileNotFoundError(f"File {dem} could not be located.")

        super().__init__(
            bbox=bbox,
            crs=crs,
            dx=self.meta["transform"][0],
            dy=abs(
                self.meta["transform"][4]
            ),  # Note: grid spacing in y-direction is negative.
            values=np.fliplr(topobathy),  # we need to flip the array
            extrapolate=extrapolate,  # user-specified potentially "dangerous" option
        )
        super().build_interpolant()

    def flip(self):
        """Flip the DEM upside down"""
        self.values = -self.values
        super().build_interpolant()
        return self

    def plot(self, coarsen=1, holding=False, **kwargs):
        """Visualize the DEM"""
        fig, ax, pc = super().plot(
            coarsen=coarsen,
            holding=True,
            cmap="terrain",
            **kwargs,
        )
        ax.set_xlabel("X-coordinate")
        ax.set_ylabel("Y-coordinate")
        ax.set_aspect("equal")
        cbar = fig.colorbar(pc)
        cbar.set_label("Topobathymetric depth (m)")
        if not holding:
            plt.show()
        return fig, ax
