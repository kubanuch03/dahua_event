from logger import get_logger

logger = get_logger("TRAFFIC INFO")


class TrafficCallBackAlarmInfo:
    def __init__(self):
        self.time_str = ""
        self.plate_number_str = ""
        self.plate_color_str = ""
        self.object_subType_str = ""
        self.vehicle_color_str = ""

    def get_alarm_info(self, alarm_info):
        self.time_str = (
            f"{alarm_info.UTC.dwYear}-{alarm_info.UTC.dwMonth:02}-{alarm_info.UTC.dwDay:02} "
            f"{alarm_info.UTC.dwHour:02}:{alarm_info.UTC.dwMinute:02}:{alarm_info.UTC.dwSecond:02}"
        )

        self.plate_number_str = self._decode_string(
            alarm_info.stTrafficCar.szPlateNumber, encoding="gb2312"
        )
        self.plate_color_str = self._decode_string(alarm_info.stTrafficCar.szPlateColor)
        self.object_subType_str = self._decode_string(
            alarm_info.stuVehicle.szObjectSubType
        )
        self.vehicle_color_str = self._decode_string(
            alarm_info.stTrafficCar.szVehicleColor
        )

        return self._get_alarm_info_dict()

    def _decode_string(self, byte_string, encoding="utf-8"):
        return str(byte_string, encoding)

    def _get_alarm_info_dict(self):
        return {
            "time_str": self.time_str,
            "plate_number_str": self.plate_number_str,
            "plate_color_str": self.plate_color_str,
            "object_subType_str": self.object_subType_str,
            "vehicle_color_str": self.vehicle_color_str,
        }
