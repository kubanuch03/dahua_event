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
    camera_alias=None,
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
        camera_alias=camera_alias,
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

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stopping the monitoring...")

    sdk.StopLoadPic(attachID)
    sdk.Logout(loginID)
    sdk.Cleanup()
    logger.info("Cleaned up and exited successfully.")
