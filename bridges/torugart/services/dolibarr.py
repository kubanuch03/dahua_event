import requests
import json
from logger import get_logger, mask_secrets
from services import config
from typing import Any, List, Optional

logger = get_logger("DOLIBARR")


class Dolibarr:
    def __init__(
        self,
        host: str,
        entrypoint_get_camera_list: str,
        api_key: str,
        entrypoint_push_event: str,
    ) -> None:
        self.host: str = host
        self.entrypoint_get_camera_list: str = entrypoint_get_camera_list
        self.entrypoint_push_event: str = entrypoint_push_event
        self.api_key: str = api_key
        self.camera_data: Optional[List[dict]] = None

    def push_event(self, event_data: dict):
        headers = {"DOLAPIKEY": self.api_key}
        response: requests.Response = requests.post(
            f"{self.host}/{self.entrypoint_push_event}",
            headers=headers,
            json=event_data,
            timeout=60,
        )

        if response.status_code == 200:
            logger.debug("Event successfully pushed.")
        else:
            logger.error(
                f"Failed to push event. Status code: {response.status_code}, Response: {response.text}"
            )
        return response

    def get_camera_list(self) -> Optional[List[dict]]:
        """Fetches camera data from the API with DOLAPIKEY."""
        headers = {"DOLAPIKEY": self.api_key}
        response: requests.Response = requests.get(
            f"{self.host}/{self.entrypoint_get_camera_list}", headers=headers, timeout=10
        )
        if response.status_code == 200:
            try:
                self.camera_data = response.json()
            except ValueError:
                # 30.10: Dolibarr перед JSON может печатать мусор (HTTP 200) - берём JSON с первой скобки
                text = response.text
                starts = [i for i in (text.find("["), text.find("{")) if i >= 0]
                if not starts:
                    logger.error("Camera list is not JSON")
                    return None
                self.camera_data = json.loads(text[min(starts):])
                logger.warning("Camera list had a non-JSON prefix, stripped")
            logger.debug("Camera data fetched successfully.")
            return self.camera_data
        else:
            logger.error(f"Error fetching data: {response.status_code}")
            return None

    def extract_camera_fields(self) -> Optional[List[dict]]:
        """Extract specific fields from the camera data."""
        if not self.camera_data:
            logger.warning("No camera data available. Fetching camera data.")
            self.get_camera_list()

        if self.camera_data:
            extracted_data = []
            for item in self.camera_data:
                logger.debug(f"Processing camera data: {mask_secrets(item)}")
                cfg = config.get()
                needed = ("username", "password", "porthttp", "portrtsp", "entrypointrtsp", "ipaddress")
                if (
                    item.get("status") == 1
                    and item.get("code") == cfg.camera_code
                    and not (cfg.require_dolibarr_fields and any(item.get(k) is None for k in needed))
                ):
                    from_item = cfg.creds_from_dolibarr
                    extracted_data.append(
                        {
                            "status": item["status"],
                            "id": item["id"],
                            "code": item["code"],
                            "username": (item.get("username") or cfg.camera_user) if from_item else cfg.camera_user,
                            "password": (item.get("password") or cfg.camera_password) if from_item else cfg.camera_password,
                            "porthttp": "80",
                            "portrtsp": "554",
                            "entrypointrtsp": item.get("entrypointrtsp") or "",
                            "ipaddress": cfg.camera_ip,
                            "port": cfg.camera_port,
                        }
                    )

                else:
                    logger.debug(f"Camera data skipped: {mask_secrets(item)}")

            if extracted_data:
                logger.debug("Camera fields extracted successfully.")
                return extracted_data
            else:
                logger.warning("No camera fields matched the criteria.")
                return None
        else:
            logger.error("Failed to extract camera fields due to missing camera data.")
            return None
