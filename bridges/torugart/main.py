from services.traffic_monitor import start_traffic_monitor
from logger import setup_logging, get_logger, mask_secrets
from services import config
from services.dolibarr import Dolibarr
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

setup_logging()

logger = get_logger("MAIN")
cfg = config.get()
logger.info({"bridge_config": cfg.describe()})
dolibarr = Dolibarr(
    host=cfg.dolibarr_url,
    entrypoint_get_camera_list="cameras?sortfield=t.rowid&sortorder=ASC&limit=100",
    entrypoint_push_event="autovisionhistorys",
    api_key=cfg.dolibarr_key,
)


def start_monitor_for_camera(camera):
    try:
        camera_ip = camera["ipaddress"]
        camera_port = camera.get("port", 37777)
        camera_username = camera.get("username", cfg.camera_user)
        camera_password = camera["password"]
        camera_code = camera.get("code", 0)
        camera_id = camera.get("id", 0)

        logger.info(
            f"Starting traffic monitor for camera {camera['code']} at {camera_ip}:{camera_port}"
        )
        start_traffic_monitor(
            camera_ip,
            camera_port,
            camera_username,
            camera_password,
            camera_code,
            camera_id,
        )

    except Exception as e:
        logger.error(f"Failed to start monitor for camera {camera['code']}: {e}")
        return camera["code"], False
    return camera["code"], True


def start_monitor_for_cameras_parallel(camera_fields):
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_camera = {
            executor.submit(start_monitor_for_camera, camera): camera
            for camera in camera_fields
        }

        for future in as_completed(future_to_camera):
            camera = future_to_camera[future]
            try:
                camera_id, result = future.result()
                if result:
                    logger.info(
                        f"Successfully started traffic monitor for camera {camera_id}"
                    )
                else:
                    logger.error(
                        f"Failed to start traffic monitor for camera {camera_id}"
                    )
            except Exception as exc:
                logger.error(f"Camera {camera['code']} generated an exception: {exc}")


if __name__ == "__main__":
    # Fetch and extract the camera data
    camera_fields = dolibarr.extract_camera_fields()
    if camera_fields:
        logger.info(json.dumps(mask_secrets(camera_fields), indent=4))
        start_monitor_for_cameras_parallel(camera_fields)
    else:
        logger.error("No camera data available to start traffic monitor.")
