
import warnings
import itertools

from utils import cached_property
from common import DataBuffer, TrackPoint, LogPoint, AvgMax


SEGMENT_BEFORE_MOVING = 0
SEGMENT_BEFORE_AUTOPAUSE = 1
SEGMENT_BEFORE_MANUALPAUSE = 2
SEGMENT_LAST = 3


OFFSET_TRACKPOINTS = 0xAB000 + 24
OFFSET_LOGPOINTS = 0x121000 + 24
OFFSET_SUMMARIES = 0x83000
OFFSET_TRACKLIST = 0x7ac86
OFFSET_TRACK_SETTINGS = 0x167000



class Rider40(object):

    READ_DATA = 0x10
    READ_SERIAL = 0x03

    BLOCK_SIZE = 4096
    BLOCK_COUNT = 0x1ff


    def __init__(self, device_access):
        self.dev = device_access


    def read_serial(self):

        data = self.dev.read_addr(0, block_count=4, read_type=self.READ_SERIAL)

        return data[-16:].tostring()


    def read_block(self, block_nr):

        if block_nr > self.BLOCK_COUNT:
            raise IOError('Reading past end of device.')

        return self.dev.read_addr(block_nr, 8, read_type=self.READ_DATA)


    def offset_to_block(self, offset):
        return offset / self.BLOCK_SIZE


    def read_from_offset(self, offset):

        d = self.read_block(self.offset_to_block(offset))

        rel_offset = offset % self.BLOCK_SIZE

        abs_offset = offset - rel_offset

        return DataBuffer(self, d, rel_offset, abs_offset)



class Track(object):

    name = None
    timestamp = None
    lap_count = None


    _offset_trackpoints = None
    _offset_summary = None
    _offset_laps = None


    def __init__(self, device):
        self.device = device


    @cached_property
    def trackpoints(self):

        buf = self.device.read_from_offset(OFFSET_TRACKPOINTS +
                                           self._offset_trackpoints)

        return _read_trackpoint_segments(buf)


    @cached_property
    def logpoints(self):
        buf = None
        segments = []
        for tseg in self.trackpoints:

            offset = OFFSET_LOGPOINTS + tseg._offset_logpoints


            if buf is None:
                buf = self.device.read_from_offset(offset)
            elif buf.abs_offset + buf.rel_offset != offset:
                warnings.warn('Unexpected logpoint offset.', RuntimeWarning)
                buf = self.device.read_from_offset(offset)


            seg = _read_logpoint_segment(buf)

            if seg.segment_type != tseg.segment_type:
                raise RuntimeError('Matching segments are expected to have'
                                   ' the same type.')

            segments.append(seg)

        return segments


    @cached_property
    def summary(self):
        buf = self.device.read_from_offset(OFFSET_SUMMARIES +
                                           self._offset_summary)
        return _read_summary(buf)


    @cached_property
    def settings(self):
        pass


    def merged_segments(self, remove_empty_track_segs=True):

        for tseg, lseg in zip(self.trackpoints, self.logpoints):

            if remove_empty_track_segs and not tseg:
                continue
            yield _merge_segments(tseg, lseg)




class Summary(object):

    start = None
    end = None
    distance = None

    speed = None
    heartrate = None
    cadence = None
    watts = None
    calories = None

    altitude_gain = None
    altitude_loss = None

    ride_time = None



class _Segment(object):


    @property
    def segment_type(self):
        return self._segment_type


    @segment_type.setter
    def segment_type(self, value):
        if value not in self._SEGMENT_TYPES:
            raise RuntimeError('Unknown type ({:x}) for {}'.
                               format(value, self.__class__.__name__))
        self._segment_type = self._SEGMENT_TYPES.index(value)



class TrackPointSegment(list, _Segment):

    _SEGMENT_TYPES = (0, 1, 2, 3)

    timestamp = None
    _offset_logpoints = None



class LogPointSegment(list, _Segment):

    _SEGMENT_TYPES = (0x02, 0x06, 0x0A, 0x0E)

    timestamp = None



def read_history(device):

    buf = device.read_from_offset(OFFSET_TRACKLIST)

    count = buf.uint16_from(0x08)

    buf.set_offset(24)

    history = []

    for i in range(count):

        timestamp = buf.uint32_from(0x00)
        name_len = buf.uint16_from(0x26)

        if timestamp == 0xffffffff:
            # It's a planned trip
            buf.set_offset(0x30 + name_len)
            continue

        t = Track(device)
        t.name = buf.str_from(0x30, name_len)
        t.timestamp = timestamp
        t.lap_count = buf.uint8_from(0x18)
        # t._bike_type = buf.uint16_from(0x04)
        t._offset_trackpoints = buf.uint32_from(0x08)
        t._offset_summary = buf.uint32_from(0x0C)
        if t.lap_count > 0:
            t._offset_laps = buf.uint32_from(0x10)


        buf.set_offset(0x30 + name_len)

        history.append(t)

    return history




def _read_trackpoint_segments(buf):

    segments = []

    while True:
        seg, next_offset = _read_trackpoint_segment(buf)

        segments.append(seg)

        if seg.segment_type == SEGMENT_LAST:
            break


        next_offset += OFFSET_TRACKPOINTS

        # Sometimes is seems like an extra trackpoint is added
        # to a segment but is not included in the count in the segment.
        # We have to check this and skip some bytes if necessary.
        if buf.abs_offset + buf.rel_offset != next_offset:

            diff = next_offset - buf.abs_offset - buf.rel_offset
            if diff > 6:
                warnings.warn('Bigger than expected diff between segment '
                              'offsets.', RuntimeWarning)
            if diff < 0:
                warnings.warn('Unexpected negative diff between segment '
                              'offsets.', RuntimeWarning)

            buf.set_offset(diff)


    return segments


def _read_trackpoint_segment(buf):

    s = TrackPointSegment()

    s.timestamp = buf.uint32_from(0x00)
    s.segment_type = buf.uint8_from(0x1A)

    lon_start = buf.int32_from(0x04)
    lat_start = buf.int32_from(0x08)
    elevation_start = (buf.uint16_from(0x14) - 4000) / 4.0

    count = buf.uint32_from(0x20)

    s._offset_logpoints = buf.uint32_from(0x24)

    if s.segment_type == SEGMENT_BEFORE_MOVING and count > 0:
        warnings.warn("Segment type {} is not expected to "
                      "have any trackpoints".format(SEGMENT_BEFORE_MOVING),
                      RuntimeWarning)

    next_offset = buf.uint32_from(0x1c)


    format = buf.uint16_from(0x18)

    if format != 0x0140:
        raise RuntimeError('Unknown trackpoint format. '
                           'It can probably easily be fixed if test data '
                           'is provided.')

    buf.set_offset(0x28)

    if count > 0:
        s.extend(_read_trackpoints(buf, s.timestamp, lon_start, lat_start,
                                   elevation_start, count))

    return s, next_offset



def _read_trackpoints(buf, time, lon, lat, ele, count):

    track_points = []
    track_points.append(TrackPoint(
        timestamp=time,
        longitude=lon / 1000000.0,
        latitude=lat / 1000000.0,
        elevation=ele
    ))

    for i in range(count):

        time += buf.uint8_from(0) / 4
        ele += buf.int8_from(0x1) / 10.0
        lon += buf.int16_from(0x02)
        lat += buf.int16_from(0x04)

        track_points.append(TrackPoint(
            timestamp=time,
            longitude=lon / 1000000.0,
            latitude=lat / 1000000.0,
            elevation=ele
        ))


        buf.set_offset(0x6)


    return track_points



def _read_logpoint_segment(buf):

    s = LogPointSegment()

    s.timestamp = buf.uint32_from(0)
    s.segment_type = buf.uint8_from(0x0c)

    count = buf.uint16_from(0x0a)

    format = buf.uint16_from(0x08)

    buf.set_offset(0x10)

    if count > 0:

        if format == 0x7104:
            log_points = _read_logpoints_format_1(buf, s.timestamp, count)
        elif format == 0x7704:
            log_points = _read_logpoints_format_2(buf, s.timestamp, count)
        else:
            raise RuntimeError('Unknown logpoint format. You are probably '
                               'using a sensor that has not been tested '
                               'during development. Maybe a powermeter.'
                               'It can probably easily be fixed if test data '
                               'is provided.')

        s.extend(log_points)

    return s



def _read_logpoints_format_1(buf, time, count):

    log_points = []

    for i in range(count):

        lp = LogPoint(
            timestamp=time,
            speed=buf.uint8_from(0x00) / 8.0 * 60 * 60 / 1000,
            temperature=buf.int16_from(0x01) / 10.0,
            airpressure=buf.uint16_from(0x03) * 2.0
        )

        log_points.append(lp)

        time += 4

        buf.set_offset(0x6)


    return log_points


def _read_logpoints_format_2(buf, time, count):

    log_points = []

    for i in range(count):

        lp = LogPoint(
            timestamp=time,
            speed=buf.uint8_from(0x00) / 8.0 * 60 * 60 / 1000,
            temperature=buf.uint16_from(0x03) / 10.0,
            airpressure=buf.uint16_from(0x05) * 2.0
        )

        cad = buf.uint8_from(0x01)
        if cad != 0xff:
            lp.cadence = cad

        hr = buf.uint8_from(0x02)
        if hr != 0xff:
            lp.heartrate = hr

        log_points.append(lp)

        time += 4

        buf.set_offset(0x8)


    return log_points



def _read_summary(buf):

    s = Summary()

    s.start = buf.uint32_from(0x00)
    s.end = buf.uint32_from(0x04)
    s.distance = buf.uint32_from(0x08)

    s.speed = AvgMax(
        buf.uint8_from(0x0c) / 8.0 * 60 * 60 / 1000,
        buf.uint8_from(0x0d) / 8.0 * 60 * 60 / 1000,
    )

    s.heartrate = AvgMax(
        buf.uint8_from(0x0e),
        buf.uint8_from(0x0f),
    )

    s.cadence = AvgMax(
        buf.uint8_from(0x10),
        buf.uint8_from(0x11),
    )

    # s.watts = AvgMax(
    #     buf.uint8_from(0x12),
    #     buf.uint8_from(0x13),
    # )

    s.altitude_gain = buf.uint16_from(0x16)
    s.altitude_loss = buf.uint16_from(0x18)
    s.calories = buf.uint16_from(0x1a)
    s.ride_time = buf.uint32_from(0x1c)

    return s



def _merge_segments(track_seg, log_seg):
    """
    The trackpoints and logpoints doesn't allways have the same timestamp.
    This function will try to merge the points which have the closest
    timestamp to eachother. Points are only merged if they are 2 or less
    seconds apart.

    This current implementation is quite ugly.

    Here is a short explanation:

    The two segments are merged into one list and sorted by timestamp.
    Then 4 items at the time are compared.

    The 3 first items are potentialy merged, the last is just used
    to check that the last to are not equal.

    The items which are closest together out of (0 and 1) or (1 and 2)
    are merged. If (1 and 2) are closest 0 is returned alone.
    """

    def _point(a, b):

        if isinstance(a, TrackPoint):
            return (a, b)
        elif isinstance(b, TrackPoint) or b is None:
            return (b, a)
        return (a, b)

    items = sorted(itertools.chain(track_seg, log_seg),
                   key=lambda x: x.timestamp)

    l = items[0:4]
    count = i = 4
    while count > 1:

        if l[0].timestamp == l[1].timestamp:
            yield _point(l.pop(0), l.pop(0))
        elif l[1].timestamp - l[0].timestamp > 2:
            yield _point(l.pop(0), None)
        elif type(l[0]) == type(l[1]):
            yield _point(l.pop(0), None)
        elif count > 2 and type(l[1]) == type(l[2]):
            yield _point(l.pop(0), l.pop(0))
        elif count > 3 and l[2].timestamp == l[3].timestamp:
            yield _point(l.pop(0), l.pop(0))
        elif count > 2:
            diff1 = l[1].timestamp - l[0].timestamp
            diff2 = l[2].timestamp - l[1].timestamp
            if diff1 > diff2:
                yield _point(l.pop(0), None)
                yield _point(l.pop(0), l.pop(0))
            else:
                yield _point(l.pop(0), l.pop(0))
        else:
            if l[1].timestamp - l[0].timestamp <= 2:
                yield _point(l.pop(0), l.pop(0))
            else:
                yield _point(l.pop(0), None)
                yield _point(l.pop(0), None)

        more = 4 - len(l)
        l.extend(items[i:i + more])  # Add back as many as was removed
        i += more
        count = len(l)

    if l:
        yield _point(l[0], None)




