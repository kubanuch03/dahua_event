import threading
import time
import os
from services.traffic_info import TrafficCallBackAlarmInfo
from services.autovision import StateNumberDetector, av_mode

from NetSDK.SDK_Enum import *
from NetSDK.SDK_Callback import *
from logger import get_logger

detector = StateNumberDetector()
logger = get_logger("CALLBACKS")
PIPELINE = None


def set_pipeline(p):
    """Подключает обработчик событий (bridge.pipeline.Pipeline)."""
    global PIPELINE
    PIPELINE = p


callback_num = 0
# shorter plates (3 chars or less) are recognition garbage, they are not sent
MIN_PLATE_LEN = 4


class Callbacks:
    camera_code = None
    camera_id = None

    @classmethod
    def set_camera_info(cls, camera_code, camera_id):
        cls.camera_code = camera_code
        cls.camera_id = camera_id

    @CB_FUNCTYPE(
        None,
        C_LLONG,
        C_DWORD,
        c_void_p,
        POINTER(c_ubyte),
        C_DWORD,
        C_LDWORD,
        c_int,
        c_void_p,
    )
    def AnalyzerDataCallBack(
        lAnalyzerHandle,
        dwAlarmType,
        pAlarmInfo,
        pBuffer,
        dwBufSize,
        dwUser,
        nSequence,
        reserved=None,
    ):
        global camera_code, camera_id
        camera_code = Callbacks.camera_code
        camera_id = Callbacks.camera_id
        global callback_num
        if dwAlarmType == EM_EVENT_IVS_TYPE.TRAFFICJUNCTION:
            local_path = os.path.abspath("data/")
            os.makedirs(local_path, exist_ok=True)
            global_dir = os.path.join(local_path, "Global")
            small_dir = os.path.join(local_path, "Small")
            os.makedirs(global_dir, exist_ok=True)
            os.makedirs(small_dir, exist_ok=True)

            show_info = TrafficCallBackAlarmInfo()
            callback_num += 1
            alarm_info = cast(
                pAlarmInfo, POINTER(DEV_EVENT_TRAFFICJUNCTION_INFO)
            ).contents
            a = show_info.get_alarm_info(alarm_info)
            logger.debug(a)

            is_global = False
            is_small = False

            if alarm_info.stuObject.bPicEnble:
                is_global = True
                GlobalScene_buf = cast(
                    pBuffer, POINTER(c_ubyte * alarm_info.stuObject.stPicInfo.dwOffSet)
                ).contents

                try:
                    with open(
                        os.path.join(global_dir, f"Global_Img{callback_num}.jpg"), "wb+"
                    ) as global_pic:
                        global_pic.write(bytes(GlobalScene_buf))
                    logger.debug(
                        f"Saved global image: {global_dir}Global_Img{callback_num}.jpg"
                    )
                    PIPELINE.handle(a, f"{global_dir}/Global_Img{callback_num}.jpg", callback_num, "alarm")
                except Exception as e:
                    logger.error(f"Error saving global image: {e}")

                if alarm_info.stuObject.stPicInfo.dwFileLenth > 0:
                    is_small = True
                    small_buf = pBuffer[
                        alarm_info.stuObject.stPicInfo.dwOffSet : alarm_info.stuObject.stPicInfo.dwOffSet
                        + alarm_info.stuObject.stPicInfo.dwFileLenth
                    ]

                    try:
                        with open(
                            os.path.join(small_dir, f"Small_Img{callback_num}.jpg"),
                            "wb+",
                        ) as small_pic:
                            small_pic.write(bytes(small_buf))
                        logger.debug(f"Saved small image: Small_Img{callback_num}.jpg")
                    except Exception as e:
                        logger.error(f"Error saving small image: {e}")
            elif dwBufSize > 0:
                is_global = True
                GlobalScene_buf = cast(pBuffer, POINTER(c_ubyte * dwBufSize)).contents

                try:
                    with open(
                        os.path.join(global_dir, f"Global_Img{callback_num}.jpg"), "wb+"
                    ) as global_pic:
                        global_pic.write(bytes(GlobalScene_buf))
                    logger.debug(
                        f"Saved global image (no alarm info): Global_Img{callback_num}.jpg"
                    )
                    PIPELINE.handle(a, f"{global_dir}/Global_Img{callback_num}.jpg", callback_num, "no_alarm_info")
                except Exception as e:
                    logger.error(f"Error saving global image (no alarm info): {e}")

