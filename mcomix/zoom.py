""" Handles zoom and fit of images in the main display area. """

import operator
from mcomix import constants
from mcomix.preferences import prefs
from mcomix import tools
from mcomix import box
from functools import reduce
from typing import List, Tuple, Sequence

IDENTITY_ZOOM = 1.0
IDENTITY_ZOOM_LOG = 0
USER_ZOOM_LOG_SCALE1 = 4.0
MIN_USER_ZOOM_LOG = -20
MAX_USER_ZOOM_LOG = 12


class ZoomModel(object):
    """ Handles zoom and fit modes. """

    def __init__(self) -> None:
        #: User zoom level.
        self._user_zoom_log = IDENTITY_ZOOM_LOG
        #: Image fit mode. Determines the base zoom level for an image by
        #: calculating its maximum size.
        self._fitmode = constants.ZoomMode.MANUAL
        self._scale_up = False

    def set_fit_mode(self, fitmode: int) -> None:
        if fitmode < constants.ZoomMode.BEST or \
           fitmode > constants.ZoomMode.SIZE:
            raise ValueError("No fit mode for id %d." % fitmode)
        self._fitmode = fitmode

    def get_scale_up(self) -> bool:
        return self._scale_up

    def set_scale_up(self, scale_up: bool) -> None:
        self._scale_up = scale_up

    def _set_user_zoom_log(self, zoom_log: int) -> None:
        self._user_zoom_log = min(max(zoom_log, MIN_USER_ZOOM_LOG), MAX_USER_ZOOM_LOG)

    def zoom_in(self) -> None:
        self._set_user_zoom_log(self._user_zoom_log + 1)

    def zoom_out(self) -> None:
        self._set_user_zoom_log(self._user_zoom_log - 1)

    def reset_user_zoom(self) -> None:
        self._set_user_zoom_log(IDENTITY_ZOOM_LOG)

    def get_zoomed_size(self, image_sizes: List[Sequence[int]], screen_size: Tuple[int, int],
                        distribution_axis: constants.PageAxis, do_not_transform: List[bool], prefer_same_size: bool,
                        fit_same_size: bool) -> Tuple[int, int]:
        scale_up = self._scale_up
        if prefer_same_size:
            # Preprocessing step: scale all images to the same size
            image_boxes = [box.Box(s) for s in image_sizes]
            # Scale up to the same size if this is allowed, otherwise scale down.
            if scale_up:
                # Scale up to union.
                pre_limits = box.Box.bounding_box(image_boxes).get_size()
            else:
                # Scale down to intersection.
                pre_limits = reduce(box.Box.intersect, image_boxes, image_boxes[0]).get_size()
            new_image_sizes = [tuple(tools.scale(s, ZoomModel._preferred_scale(
                s, pre_limits, distribution_axis))) for s in image_sizes]
            new_image_sizes2 = [new_image_sizes[i] if not do_not_transform[i] else image_sizes[i]
                                for i in range(len(new_image_sizes))]
            image_sizes = new_image_sizes2
        union_size = _union_size(image_sizes, distribution_axis)
        limits = ZoomModel._calc_limits(union_size, screen_size, self._fitmode,
                                        scale_up)
        prefscale = ZoomModel._preferred_scale(union_size, limits, distribution_axis)
        preferred_scales = tuple([prefscale if not dnt else IDENTITY_ZOOM for dnt in do_not_transform])
        prescaled = list(map(lambda size, scale, dnt: tuple(_scale_image_size(size, scale)),
                         image_sizes, preferred_scales, do_not_transform))
        prescaled_union_size = _union_size(prescaled, distribution_axis)

        def _other_preferences(limits: Sequence[int], distribution_axis: constants.PageAxis) -> bool:
            for i in range(len(limits)):
                if i == distribution_axis:
                    continue
                if limits[i] is not None:
                    return True
            return False
        other_preferences = _other_preferences(limits, distribution_axis)
        if limits[distribution_axis] is not None and \
            (prescaled_union_size[distribution_axis] > screen_size[distribution_axis]
            or not other_preferences):
            distributed_scales = ZoomModel._scale_distributed(image_sizes,
                distribution_axis, limits[distribution_axis], scale_up, do_not_transform)
            if other_preferences:
                preferred_scales = list(map(min, preferred_scales, distributed_scales))
            else:
                preferred_scales = distributed_scales
        if not scale_up:
            preferred_scales = [min(x, IDENTITY_ZOOM) for x in preferred_scales]
        user_scale = 2 ** (self._user_zoom_log / USER_ZOOM_LOG_SCALE1)
        res_scales = [preferred_scales[i] * (user_scale if not do_not_transform[i] else IDENTITY_ZOOM)
            for i in range(len(preferred_scales))]
        res = list(map(lambda size, scale: list(_scale_image_size(size, scale)),
            image_sizes, res_scales))
        distorted = [False] * len(res)
        if prefer_same_size and fit_same_size:
            # While the algorithm so far tries hard to keep the aspect ratios of the
            # original images, in extreme cases, it is not possible to both keep aspect
            # ratios as well as make the images fit to the same size, especially after
            # applying user_scale. In those cases, we will make them fit.
            # Simple approach: For each dimension, we fit each image to either the
            # minimum size (if scale_up is false) or maximum size (if scale_up is true)
            # of all images, given the scaled sizes computed so far.
            op = operator.gt if scale_up else operator.lt
            exs = [None] * len(limits)
            for d in range(len(limits)):
                if d == distribution_axis:
                    continue
                for i in res:
                    if exs[d] is None or op(i[d], exs[d]):
                        exs[d] = i[d]
            for d in range(len(limits)):
                if d == distribution_axis:
                    continue
                for i in range(len(res)):
                    if (res[i][d] != exs[d]) and not do_not_transform[i]:
                        res[i][d] = exs[d]
                        distorted[i] = True
        return (res, distorted)

    @staticmethod
    def _preferred_scale(image_size, limits, distribution_axis):
        """ Returns scale that makes an image of size image_size respect the
        limits imposed by limits. If no proper value can be determined,
        IDENTITY_ZOOM is returned. """
        min_scale = None
        for i in range(len(limits)):
            if i == distribution_axis:
                continue
            l = limits[i]
            if l is None:
                continue
            s = tools.div(l, image_size[i])
            if min_scale is None or s < min_scale:
                min_scale = s
        if min_scale is None:
            min_scale = IDENTITY_ZOOM
        return min_scale

    @staticmethod
    def _calc_limits(union_size, screen_size, fitmode, allow_upscaling):
        """ Returns a list or a tuple with the i-th element set to int x if
        fitmode limits the size at the i-th axis to x, or None if fitmode has no
        preference for this axis. """
        manual = fitmode == constants.ZoomMode.MANUAL
        if fitmode == constants.ZoomMode.BEST or \
            (manual and allow_upscaling and all(tools.smaller(union_size, screen_size))):
            return screen_size
        if fitmode == constants.ZoomMode.SIZE:
            if union_size[constants.PageAxis.WIDTH] > union_size[constants.PageAxis.HEIGHT]:
                return [int(prefs['fit to size width wide']),
                    int(prefs['fit to size height wide'])]
            else:
                return [int(prefs['fit to size width other']),
                    int(prefs['fit to size height other'])]
        result = [None] * len(screen_size)
        if not manual:
            fixed_size = None
            if fitmode == constants.ZoomMode.WIDTH:
                axis = constants.PageAxis.WIDTH
            elif fitmode == constants.ZoomMode.HEIGHT:
                axis = constants.PageAxis.HEIGHT
            else:
                assert False, 'Cannot map fitmode to axis'
            result[axis] = fixed_size if fixed_size is not None else screen_size[axis]
        return result

    @staticmethod
    def _scale_distributed(sizes, axis, max_size, allow_upscaling,
        do_not_transform):
        """ Calculates scales for a list of boxes that are distributed along a
        given axis (without any gaps). If the resulting scales are applied to
        their respective boxes, their new total size along axis will be as close
        as possible to max_size. The current implementation ensures that equal
        box sizes are mapped to equal scales.
        @param sizes: A list of box sizes.
        @param axis: The axis along which those boxes are distributed.
        @param max_size: The maximum size the scaled boxes may have along axis.
        @param allow_upscaling: True if upscaling is allowed, False otherwise.
        @param do_not_transform: True if the resulting scale must be 1, False
        otherwise.
        @return: A list of scales where the i-th scale belongs to the i-th box
        size. If sizes is empty, the empty list is returned. If there are more
        boxes than max_size, an approximation is returned where all resulting
        scales will shrink their respective boxes to 1 along axis. In this case,
        the scaled total size might be greater than max_size. """
        n = len(sizes)
        # trivial cases first
        if n == 0:
            return []
        if n >= max_size:
            # In this case, only one solution or only an approximation is available.
            # if n > max_size, the result won't fit into max_size.
            return [IDENTITY_ZOOM if dnt else tools.div(1, s[axis]) for s, dnt in zip(sizes, do_not_transform)]
        total_axis_size = sum([s[axis] for s in sizes])
        total_dnt_axis_size = sum([s[axis] for s, dnt in zip(sizes, do_not_transform) if dnt])
        if ((total_axis_size <= max_size) and not allow_upscaling) or \
            (total_axis_size == total_dnt_axis_size):
            # identity
            return [IDENTITY_ZOOM] * n

        # non-trival case
        # initial guess
        scale = tools.div(max_size - total_dnt_axis_size, total_axis_size - total_dnt_axis_size)
        scaling_data = [None] * n
        total_axis_size = 0
        # This loop collects some data we need for the actual computations later.
        for i in range(n):
            this_size = sizes[i]
            # Shortcut: If the size cannot be changed, accept the original size.
            if do_not_transform[i]:
                total_axis_size += this_size[axis]
                scaling_data[i] = [IDENTITY_ZOOM, IDENTITY_ZOOM, False,
                    IDENTITY_ZOOM, 0.0]
                continue
            # Initial guess: The current scale works for all tuples.
            ideal = tools.scale(this_size, scale)
            ideal_vol = tools.volume(ideal)
            # Let's use a dummy to compute the actual (rounded) size along axis
            # so we can rescale the rounded tuple with a better local_scale
            # later. This rescaling is necessary to ensure that the sizes in ALL
            # dimensions are monotonically scaled (with respect to local_scale).
            # A nice side effect of this is that it keeps the aspect ratio better.
            dummy_approx = _round_nonempty((ideal[axis],))[0]
            local_scale = tools.div(dummy_approx, this_size[axis])
            total_axis_size += dummy_approx
            can_be_downscaled = dummy_approx > 1
            if can_be_downscaled:
                forced_size = dummy_approx - 1
                forced_scale = tools.div(forced_size, this_size[axis])
                forced_approx = _scale_image_size(this_size, forced_scale)
                forced_vol_err = tools.relerr(tools.volume(forced_approx), ideal_vol)
            else:
                forced_scale = None
                forced_vol_err = None
            scaling_data[i] = [local_scale, ideal, can_be_downscaled,
                forced_scale, forced_vol_err]
        # Now we need to find at most total_axis_size - max_size occasions to
        # scale down some tuples so the whole thing would fit into max_size. If
        # we are lucky, there will be no gaps at the end (or at least fewer gaps
        # than we would have if we always rounded down).
        dirty=True # This flag prevents infinite loops if nothing can be made any smaller.
        while dirty and (total_axis_size > max_size):
            # This algorithm needs O(n*n) time. Let's hope that n is small enough.
            dirty=False
            current_index = 0
            current_min = None
            for i in range(n):
                d = scaling_data[i]
                if not d[2]:
                    # Ignore elements that cannot be made any smaller.
                    continue
                if (current_min is None) or (d[4] < current_min[4]):
                    # We are searching for the tuple where downscaling results
                    # in the smallest relative volume error (compared to the
                    # respective ideal volume).
                    current_min = d
                    current_index = i
            for i in range(current_index, n):
                # We must scale down ALL equal tuples. Otherwise, images that
                # are of equal size might appear to be of different size
                # afterwards. The downside of this approach is that it might
                # introduce more gaps than necessary.
                d = scaling_data[i]
                if (not d[2]) or (d[1] != current_min[1]):
                    continue
                d[0] = d[3]
                d[2] = False # only once per tuple
                total_axis_size -= 1
                dirty=True
        else:
            # If we are here and total_axis_size < max_size, we could try to
            # upscale some tuples similarly to the other loop (i.e. smallest
            # relative volume error first, equal boxes in conjunction with each
            # other). However, this is not as useful as the other loop, slightly
            # more complicated and it won't do anything if all tuples are equal.
            pass
        return [d[0] for d in scaling_data]

def _scale_image_size(size, scale):
    return _round_nonempty(tools.scale(size, scale))

def _round_nonempty(t):
    result = [0] * len(t)
    for i in range(len(t)):
        x = int(round(t[i]))
        result[i] = x if x > 0 else 1
    return result

def _union_size(image_sizes, distribution_axis):
    if len(image_sizes) == 0:
        return []
    n = len(image_sizes[0])
    union_size = [reduce(max, [x[i] for x in image_sizes]) for i in range(n)]
    union_size[distribution_axis] = sum([x[distribution_axis] for x in image_sizes])
    return union_size

# vim: expandtab:sw=4:ts=4
