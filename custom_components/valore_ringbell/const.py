"""Integration constants."""

from homeassistant.const import Platform

DOMAIN = "valore_ringbell"
PLATFORMS = [Platform.CAMERA, Platform.EVENT]
CONF_COUNTRY_CODE = "country_code"
CONF_DEVICE_NAME = "device_name"
CONF_WAITING_TONE = "waiting_tone"
