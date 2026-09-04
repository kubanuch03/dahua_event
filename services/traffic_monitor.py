from NetSDK.NetSDK import NetClient
from services.callbacks import Callbacks
from NetSDK.SDK_Enum import EM_LOGIN_SPAC_CAP_TYPE, EM_EVENT_IVS_TYPE
from NetSDK.SDK_Callback import (
    NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY,
    NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY,
)
import time
from logger import get_logger
from ctypes import CFUNCTYPE, c_int, c_void_p, byref, sizeof
logger = get_logger("TRAFFIC MONITOR")

# Определяем тип коллбэка
fHaveReConnect = CFUNCTYPE(None, c_int, c_void_p)

# Функция-обработчик переподключения
def reconnect_callback(loginID, user_data):
    logger.info(f"==================")
    logger.info(f"[INFO] Successfully reconnected! Login ID: {loginID}")
    logger.info(f"==================")

# Конвертируем функцию в C-совместимый формат
reconnect_callback_c = fHaveReConnect(reconnect_callback)


def start_traffic_monitor(
    camera_ip,
    camera_port,
    camera_username,
    camera_password,
    camera_code,
    camera_name, # <-- Правильный порядок
    camera_id, # <-- Правильный порядок
    camera_routing_key=None,
    ) -> None:
    sdk = NetClient()
    sdk.InitEx(None)
    sdk.SetAutoReconnect(reconnect_callback_c, None)

    stuInParam = NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY()
    stuInParam.dwSize = sizeof(NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY)
    stuInParam.szIP = camera_ip.encode()
    stuInParam.nPort = camera_port
    stuInParam.szUserName = camera_username.encode()
    stuInParam.szPassword = camera_password.encode()
    stuInParam.emSpecCap = EM_LOGIN_SPAC_CAP_TYPE.TCP
    stuInParam.pCapParam = None

    stuOutParam = NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY()
    stuOutParam.dwSize = sizeof(NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY)

    loginID, device_info, error_msg = sdk.LoginWithHighLevelSecurity(
        stuInParam, stuOutParam
    )

    if not loginID:
        logger.error(f"Login failed: {error_msg}")
        return

    logger.info(f"Login successful. Channels available: {device_info.nChanNum}")
    callbacks = Callbacks()
    callbacks.set_camera_info(
        camera_code=camera_code, camera_id=camera_id, camera_name=camera_name,
        camera_routing_key=camera_routing_key,
    )
    channel = 0
    attachID = sdk.RealLoadPictureEx(
        loginID,
        channel,
        EM_EVENT_IVS_TYPE.TRAFFICJUNCTION,
        1,
        callbacks.AnalyzerDataCallBack,
        0,
        None,
    )

    if not attachID:
        logger.error(f"Subscription failed: {sdk.GetLastError()}")
        sdk.Logout(loginID)
        return

    logger.info(
        "Subscription to traffic junction events successful. Monitoring started."
    )

    # Drain-поток durable-outbox запускается в main.py, ДО этой функции и
    # ДО попытки логина в камеру (см. main.py) - раздача событий из
    # очереди в SmartParking логически не зависит от того, доступна ли
    # прямо сейчас сама камера. Раньше он стартовал только здесь, после
    # успешного логина/подписки - при неверных кредах камеры (реальный,
    # часто повторяющийся сценарий на этом объекте) уже накопленные в
    # outbox события не добивались никогда, даже если SmartParking был
    # прекрасно доступен.

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stopping the monitoring...")

    sdk.StopLoadPic(attachID)
    sdk.Logout(loginID)
    sdk.Cleanup()
    logger.info("Cleaned up and exited successfully.")
