import re

from logger import get_logger

logger = get_logger("TRAFFIC INFO")

# KG plates: camera reads "01123ABC", region 01-10 must become "01KG123ABC"
KG_PLATE_RE = re.compile(r"^(0[1-9]|10)(\d{3}[A-Z]{3})$")


class TrafficCallBackAlarmInfo:
    def __init__(self):
        self.time_str = ""
        self.plate_number_str = ""
        self.plate_color_str = ""
        self.object_subType_str = ""
        self.vehicle_color_str = ""
        self.plate_conf = -1
        self.view = {}

    def get_alarm_info(self, alarm_info):
        self.time_str = (
            f"{alarm_info.UTC.dwYear}-{alarm_info.UTC.dwMonth:02}-{alarm_info.UTC.dwDay:02} "
            f"{alarm_info.UTC.dwHour:02}:{alarm_info.UTC.dwMinute:02}:{alarm_info.UTC.dwSecond:02}"
        )

        self.plate_number_str = self._decode_string(
            alarm_info.stTrafficCar.szPlateNumber, encoding="gb2312"
        )
        self.plate_number_str = self._add_kg_region(self.plate_number_str)
        self.plate_color_str = self._decode_string(alarm_info.stTrafficCar.szPlateColor)
        self.object_subType_str = self._decode_string(
            alarm_info.stuVehicle.szObjectSubType
        )
        self.vehicle_color_str = self._decode_string(
            alarm_info.stTrafficCar.szVehicleColor
        )

        # уверенность распознавания номера камерой, 0..100 (AutoVision, 19.10)
        self.plate_conf = int(getattr(alarm_info.stuObject, "nConfidence", -1))

        self.view = self._read_view(alarm_info)

        return self._get_alarm_info_dict()

    #: byVehicleDirection (EM_VEHICLE_DIRECTION): что попало в кадр
    _VEHICLE_VIEW = {1: "head", 2: "tail", 3: "side"}
    #: emVehiclePosture (EM_VEHICLE_POSTURE_TYPE): поза машины
    _VEHICLE_POSTURE = {1: "head", 2: "side", 3: "tail"}
    #: emCarDrivingDirection (EM_CAR_DRIVING_DIRECTION): въезжает в зону / выезжает
    _DRIVING_DIR = {1: "in", 2: "out"}
    #: byDirection: прямое / обратное направление полосы
    _LANE_DIR = {1: "forward", 2: "reverse"}

    def _read_view(self, alarm_info) -> dict:
        """
        Ракурс и направление движения, как их отдала камера: голова / хвост / борт,
        въезд / выезд, полоса, скорость, передний и задний знак отдельно.

        Вызывается ВНУТРИ ctypes-колбэка: исключение здесь теряет событие целиком,
        поэтому каждое поле читается отдельно и при любой ошибке пустое. Ноль в
        enum - «камера не знает»: отдаём пустую строку, а не выдуманное значение.
        Прошивка ITC413 может не заполнять часть полей - это выясняется по данным.
        """
        out = {}

        def grab(name, getter):
            try:
                out[name] = getter()
            except Exception as exc:  # noqa: BLE001 - поле справочное
                logger.debug(f"view.{name}: {exc}")
                out[name] = "" if name not in ("lane_no", "speed_kmh") else None

        grab("vehicle_view", lambda: self._VEHICLE_VIEW.get(int(alarm_info.byVehicleDirection), ""))
        grab("vehicle_posture", lambda: self._VEHICLE_POSTURE.get(int(alarm_info.emVehiclePosture), ""))
        grab("driving_dir", lambda: self._DRIVING_DIR.get(int(alarm_info.emCarDrivingDirection), ""))
        grab("lane_dir", lambda: self._LANE_DIR.get(int(alarm_info.byDirection), ""))
        grab("lane_no", lambda: (lambda v: v if v >= 0 else None)(int(alarm_info.nLane)))
        grab("speed_kmh", lambda: (lambda v: v if v >= 0 else None)(int(alarm_info.nSpeed)))
        # Рамка номера, как её видит камера: относительные координаты (доли 8192 по каждой оси) и абсолютные.
        # Нужна, чтобы выбирать рамку модели ГЕОМЕТРИЧЕСКИ (пересечение), а не по тексту номера.
        def rect(r):
            return ",".join(str(int(getattr(r, k))) for k in ("left", "top", "right", "bottom"))

        grab("plate_box", lambda: rect(alarm_info.stuObject.BoundingBox))
        grab("plate_box_abs", lambda: rect(alarm_info.stuObject.stuOriginalBoundingBox))
        grab("plate_front", lambda: self._decode_string(alarm_info.stuPlateInfo.szFrontPlateNumber, encoding="gb2312").strip())
        grab("plate_back", lambda: self._decode_string(alarm_info.stuPlateInfo.szBackPlateNumber, encoding="gb2312").strip())
        return out

    def _add_kg_region(self, plate):
        return KG_PLATE_RE.sub(r"\1KG\2", plate)

    def _decode_string(self, byte_string, encoding="utf-8"):
        return str(byte_string, encoding)

    def _get_alarm_info_dict(self):
        return {
            "time_str": self.time_str,
            "plate_number_str": self.plate_number_str,
            "plate_color_str": self.plate_color_str,
            "object_subType_str": self.object_subType_str,
            "vehicle_color_str": self.vehicle_color_str,
            "plate_conf": self.plate_conf,
            "view": self.view,
        }
