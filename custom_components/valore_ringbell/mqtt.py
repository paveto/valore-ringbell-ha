"""Login helpers from the working Valore client."""

import hashlib
from urllib.parse import urlparse

from tuya_ipc_p2p_sdk.models import MqttIdentity
from tuya_mobile import TuyaPasswordClient
from tuya_mobile.mqtt_auth import mqtt_credentials


def md5(value):
    return hashlib.md5(value.encode()).hexdigest()


class MobileClient(TuyaPasswordClient):

    async def _submit_login(self, *args, **kwargs):

        result = await super()._submit_login(
            *args, **kwargs
        )

        self.raw_login = result
        return result


def get_mqtt_identity(mobile, login):

    raw = mobile.raw_login
    domain = raw.get("domain") or {}

    partner = (
        raw.get("partnerIdentity")
        or domain.get("partnerIdentity")
    )

    broker = (
        domain.get("mobileMqttsUrl")
        or raw.get("mobileMqttsUrl")
        or domain.get("mobileMqttUrl")
        or raw.get("mobileMqttUrl")
    )

    if not partner or not broker:
        raise RuntimeError("MQTT configuration missing")

    if "://" not in broker:
        broker = "mqtts://" + broker

    parsed = urlparse(broker)

    if not parsed.hostname:
        raise RuntimeError("Invalid MQTT broker")

    port = (
        parsed.port
        if parsed.scheme in ("mqtts", "ssl", "tls")
        and parsed.port
        else 8883
    )

    credentials = mqtt_credentials(
        mobile.signer,
        uid=login.uid,
        sid=login.sid,
        ecode=login.ecode,
        partner_id=partner
    )

    package = mobile.profile.package
    fingerprint = mobile.device_id

    client_id = (
        f"{package}_mb_"
        f"{fingerprint}_"
        f"{md5(login.uid + 'sdkfasodifca')}"
        "_DEFAULT"
    )


    return MqttIdentity(
        host=parsed.hostname,
        port=port,
        client_id=client_id,
        username=credentials["username"],
        password=credentials["password"]
    )
