from NetSDK.NetSDK import NetClient
from services.callbacks import Callbacks
from services import callbacks as callbacks_mod, autovision as autovision_mod, config
from bridge.core import Core
import logger as logger_mod
import sys
from NetSDK.SDK_Enum import EM_LOGIN_SPAC_CAP_TYPE, EM_EVENT_IVS_TYPE
from NetSDK.SDK_Callback import (
    NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY,
    NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY,
    fDisConnect,
    fHaveReConnect,
)
from ctypes import sizeof
import time
from logger import get_logger

logger = get_logger("TRAFFIC MONITOR")

LOGIN_RETRY_SEC = 30
# if the SDK has not restored the connection itself within this time, log in again
RELOGIN_AFTER_SEC = 120

# time of the last disconnect, None while the camera is connected
disconnected_at = None


@fDisConnect
def on_disconnect(login_id, ip, port, user):
    global disconnected_at
    disconnected_at = time.time()
    logger.warning(f"Connection lost: {(ip or b'').decode(errors='replace')}:{port}")


@fHaveReConnect
def on_reconnect(login_id, ip, port, user):
    global disconnected_at
    disconnected_at = None
    logger.info(f"Connection restored: {(ip or b'').decode(errors='replace')}:{port}")


def login(sdk, camera_ip, camera_port, camera_username, camera_password):
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

    return sdk.LoginWithHighLevelSecurity(stuInParam, stuOutParam)


def start_traffic_monitor(
    camera_ip: str,
    camera_port: int,
    camera_username: str,
    camera_password: str,
    camera_code: str,
    camera_id: int,
) -> None:
    global disconnected_at
    sdk = NetClient()
    sdk.InitEx(on_disconnect)
    sdk.SetAutoReconnect(on_reconnect)

    callbacks = Callbacks()
    callbacks.set_camera_info(camera_code=camera_code, camera_id=camera_id)
    channel = 0
    core = Core(config.get(), camera_code, camera_id, callbacks_mod.detector, autovision_mod.av_mode, logger_mod.RUN_ID, autovision_mod)
    callbacks_mod.set_pipeline(core.pipeline)
    core.start()

    try:
        while True:
            loginID, device_info, error_msg = login(
                sdk, camera_ip, camera_port, camera_username, camera_password
            )
            if not loginID:
                logger.error(
                    f"Login failed: {error_msg}, retry in {LOGIN_RETRY_SEC} s"
                )
                time.sleep(LOGIN_RETRY_SEC)
                continue

            logger.info(f"Login successful. Channels available: {device_info.nChanNum}")
            core.health.set_sdk(logged_in=True, disconnected_at=None)
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
                logger.error(
                    f"Subscription failed: {sdk.GetLastErrorMessage()}, retry in {LOGIN_RETRY_SEC} s"
                )
                sdk.Logout(loginID)
                time.sleep(LOGIN_RETRY_SEC)
                continue

            disconnected_at = None
            logger.info(
                "Subscription to traffic junction events successful. Monitoring started."
            )

            relogin = False
            while (disconnected_at is None or time.time() - disconnected_at < RELOGIN_AFTER_SEC) and not relogin:
                time.sleep(1)
                core.health.set_sdk(disconnected_at=disconnected_at)
                last = core.journal.last_ts
                if last is not None and core.watchdog.anchor < last:
                    core.watchdog.note_event(last)
                act = core.watchdog.tick(connected=disconnected_at is None)
                if act == "resubscribe":
                    logger.warning("Watchdog: no events, resubscribing")
                    sdk.StopLoadPic(attachID)
                    attachID = sdk.RealLoadPictureEx(
                        loginID, channel, EM_EVENT_IVS_TYPE.TRAFFICJUNCTION, 1, callbacks.AnalyzerDataCallBack, 0, None
                    )
                    core.health.set_sdk(resubscribes_inc=1)
                elif act == "relogin":
                    logger.warning("Watchdog: no events after resubscribe, logging in again")
                    relogin = True
                elif act == "exit":
                    logger.error("Watchdog: no events after relogin, exiting")
                    sys.exit(3)
            core.health.set_sdk(logged_in=False)

            if not relogin:
                logger.warning(
                    f"No connection for {RELOGIN_AFTER_SEC} s, logging in again"
                )
            sdk.StopLoadPic(attachID)
            sdk.Logout(loginID)
    except KeyboardInterrupt:
        logger.info("Stopping the monitoring...")

    sdk.Cleanup()
    logger.info("Cleaned up and exited successfully.")
