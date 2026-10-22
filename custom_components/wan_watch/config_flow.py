from __future__ import annotations

import copy
import ipaddress

import asyncssh
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    DEFAULT_DOWNLOAD_TARGET,
    DEFAULT_SETTINGS,
    DEFAULT_TARGETS,
    DEFAULT_UPLOAD_TARGET,
    DOMAIN,
    EVENT_TYPES,
)
from .model import (
    ID_RE,
    validate_download_target,
    validate_targets,
    validate_topology,
    validate_upload_target,
)
from .transport import connect, discover_fingerprint


def select(options, multiple=False):
    return selector.SelectSelector(
        selector.SelectSelectorConfig(options=options, multiple=multiple)
    )


def field(name, default=None, required=True):
    cls = vol.Required if required else vol.Optional
    return cls(name, default=default) if default is not None else cls(name)


class Wizard:
    config: dict
    editing_router: str | None = None
    editing_channel: str | None = None
    candidate_router: dict

    async def async_step_menu(self, user_input=None):
        return self.async_show_menu(
            step_id="menu",
            menu_options=[
                "choose_router",
                "choose_channel",
                "settings",
                "remove_router",
                "remove_channel",
                "finish",
            ],
        )

    async def async_step_choose_router(self, user_input=None):
        if user_input is not None:
            self.editing_router = user_input["router"] if user_input["router"] != "__new" else None
            return await self.async_step_router()
        options = [{"value": "__new", "label": "Add router / Добавить роутер"}]
        options += [{"value": r["id"], "label": r["name"]} for r in self.config["routers"]]
        return self.async_show_form(
            step_id="choose_router",
            data_schema=vol.Schema(
                {
                    vol.Required("router", default="__new"): select(options),
                }
            ),
        )

    async def async_step_router(self, user_input=None):
        old = next((r for r in self.config["routers"] if r["id"] == self.editing_router), {})
        errors = {}
        if user_input is not None:
            candidate = dict(user_input)
            if not ID_RE.fullmatch(candidate["id"]) or candidate["id"] in {
                "site",
                "internet",
                "route",
            }:
                errors["base"] = "invalid_id"
            elif candidate["id"] in {c["id"] for c in self.config["channels"]} or any(
                r["id"] == candidate["id"] and r["id"] != self.editing_router
                for r in self.config["routers"]
            ):
                errors["base"] = "duplicate_id"
            elif old and candidate["id"] != old["id"]:
                errors["base"] = "immutable_id"
            else:
                for secret in ("password", "private_key"):
                    candidate[secret] = candidate.get(secret) or old.get(secret, "")
                if not candidate.get("password") and not candidate.get("private_key"):
                    errors["base"] = "credentials_required"
                else:
                    try:
                        candidate["host_key"] = await discover_fingerprint(
                            candidate["host"], candidate["port"]
                        )
                    except (OSError, asyncssh.Error, TimeoutError, ValueError):
                        errors["base"] = "cannot_connect"
                    else:
                        self.candidate_router = candidate
                        if old.get("host_key") == candidate["host_key"]:
                            return await self.async_step_trust({"confirm": True})
                        return await self.async_step_trust()
        defaults = {
            "id": "router",
            "name": "Router",
            "host": "",
            "port": 22,
            "driver": "keenetic",
            "username": "root",
            **old,
        }
        schema = {
            field("id", defaults["id"]): str,
            field("name", defaults["name"]): str,
            field("host", defaults["host"]): str,
            field("port", defaults["port"]): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
            field("driver", defaults["driver"]): select(["keenetic", "linux"]),
            field("username", defaults["username"]): str,
            field("password", "", False): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            field("private_key", "", False): selector.TextSelector(
                selector.TextSelectorConfig(multiline=True, type=selector.TextSelectorType.PASSWORD)
            ),
        }
        return self.async_show_form(step_id="router", data_schema=vol.Schema(schema), errors=errors)

    async def async_step_trust(self, user_input=None):
        errors = {}
        if user_input is not None and user_input.get("confirm"):
            try:
                conn = await connect(self.candidate_router)
                conn.close()
                await conn.wait_closed()
            except (OSError, asyncssh.Error, TimeoutError, ValueError):
                errors["base"] = "cannot_authenticate"
            else:
                self.config["routers"] = [
                    r for r in self.config["routers"] if r["id"] != self.editing_router
                ]
                self.config["routers"].append(self.candidate_router)
                if not self.config.get("egress_router"):
                    self.config["egress_router"] = self.candidate_router["id"]
                return await self.async_step_menu()
        return self.async_show_form(
            step_id="trust",
            data_schema=vol.Schema(
                {
                    vol.Required("confirm", default=False): bool,
                }
            ),
            description_placeholders={
                "fingerprint": self.candidate_router["host_key"],
                "host": self.candidate_router["host"],
            },
            errors=errors,
        )

    async def async_step_choose_channel(self, user_input=None):
        if not self.config["routers"]:
            return await self.async_step_choose_router()
        if user_input is not None:
            self.editing_channel = (
                user_input["channel"] if user_input["channel"] != "__new" else None
            )
            return await self.async_step_channel()
        options = [{"value": "__new", "label": "Add channel / Добавить канал"}]
        options += [{"value": c["id"], "label": c["name"]} for c in self.config["channels"]]
        return self.async_show_form(
            step_id="choose_channel",
            data_schema=vol.Schema(
                {
                    vol.Required("channel", default="__new"): select(options),
                }
            ),
        )

    async def async_step_channel(self, user_input=None):
        routers = [{"value": r["id"], "label": r["name"]} for r in self.config["routers"]]
        old = next((c for c in self.config["channels"] if c["id"] == self.editing_channel), {})
        errors = {}
        if user_input is not None:
            candidate = dict(user_input)
            if candidate.get("upstream_router") == "none":
                candidate["upstream_router"] = ""
            proposed = [c for c in self.config["channels"] if c["id"] != self.editing_channel] + [
                candidate
            ]
            try:
                validate_topology(self.config["routers"], proposed)
                if candidate["id"] in {"site", "internet", "route"} | {
                    r["id"] for r in self.config["routers"]
                }:
                    raise ValueError("duplicate_id")
                if old and candidate["id"] != old["id"]:
                    raise ValueError("immutable_id")
            except ValueError as exc:
                errors["base"] = str(exc)
            else:
                self.config["channels"] = proposed
                if not old:
                    self.config["settings"]["alert_channels"].append(candidate["id"])
                return await self.async_step_menu()
        defaults = {
            "id": "provider",
            "name": "Provider",
            "router_id": routers[0]["value"],
            "interface": "",
            "logical_interface": "",
            "upstream_router": "none",
            **old,
        }
        defaults["upstream_router"] = defaults.get("upstream_router") or "none"
        return self.async_show_form(
            step_id="channel",
            data_schema=vol.Schema(
                {
                    field("id", defaults["id"]): str,
                    field("name", defaults["name"]): str,
                    field("router_id", defaults["router_id"]): select(routers),
                    field("interface", defaults["interface"]): str,
                    field("logical_interface", defaults["logical_interface"], False): str,
                    field("upstream_router", defaults["upstream_router"]): select(
                        [
                            {"value": "none", "label": "Provider / Провайдер"},
                            *routers,
                        ]
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_settings(self, user_input=None):
        if not self.config["routers"]:
            return await self.async_step_choose_router()
        errors = {}
        if user_input is not None:
            try:
                validate_targets(user_input["targets"])
                validate_download_target(user_input.get("download_target", DEFAULT_DOWNLOAD_TARGET))
                validate_upload_target(user_input.get("upload_target", DEFAULT_UPLOAD_TARGET))
            except (ValueError, KeyError, TypeError, ipaddress.AddressValueError):
                errors["base"] = "invalid_targets"
            else:
                self.config["egress_router"] = user_input.pop("egress_router")
                self.config["targets"] = user_input.pop("targets")
                self.config["settings"].update(user_input)
                return await self.async_step_menu()
        settings = {**DEFAULT_SETTINGS, **self.config["settings"]}
        routers = [{"value": r["id"], "label": r["name"]} for r in self.config["routers"]]
        channels = [{"value": "internet", "label": "Whole network / Вся сеть"}]
        channels += [{"value": c["id"], "label": c["name"]} for c in self.config["channels"]]
        schema = {
            field("egress_router", self.config["egress_router"]): select(routers),
            field("interval", settings["interval"]): vol.All(
                vol.Coerce(int), vol.Range(min=15, max=3600)
            ),
            field("confirm_samples", settings["confirm_samples"]): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=20)
            ),
            field("ping_count", settings["ping_count"]): vol.All(
                vol.Coerce(int), vol.Range(min=2, max=20)
            ),
            field("loss_threshold", settings["loss_threshold"]): vol.All(
                vol.Coerce(float), vol.Range(min=0.1, max=100)
            ),
            field("latency_threshold", settings["latency_threshold"]): vol.All(
                vol.Coerce(float), vol.Range(min=1, max=10000)
            ),
            field("notify_entities", settings["notify_entities"]): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="notify", multiple=True)
            ),
            field("alert_channels", settings["alert_channels"]): select(channels, True),
            field("alert_types", settings["alert_types"]): select(EVENT_TYPES, True),
            field("persistent_notifications", settings["persistent_notifications"]): bool,
            field("targets", self.config["targets"]): selector.ObjectSelector(),
            field("upload_enabled", settings["upload_enabled"]): bool,
            field(
                "upload_target", settings.get("upload_target", DEFAULT_UPLOAD_TARGET)
            ): selector.ObjectSelector(),
            field("download_channels", settings["download_channels"]): select(
                [{"value": c["id"], "label": c["name"]} for c in self.config["channels"]], True
            ),
            field("download_interval", settings["download_interval"]): vol.All(
                vol.Coerce(int), vol.Range(min=600, max=604800)
            ),
            field("download_bytes", settings["download_bytes"]): vol.All(
                vol.Coerce(int), vol.Range(min=1000000, max=20000000)
            ),
            field(
                "download_target", settings.get("download_target", DEFAULT_DOWNLOAD_TARGET)
            ): selector.ObjectSelector(),
        }
        return self.async_show_form(
            step_id="settings", data_schema=vol.Schema(schema), errors=errors
        )

    async def async_step_remove_router(self, user_input=None):
        errors = {}
        if user_input is not None:
            rid = user_input["router"]
            if any(
                c["router_id"] == rid or c.get("upstream_router") == rid
                for c in self.config["channels"]
            ):
                errors["base"] = "router_in_use"
            elif rid == self.config.get("egress_router"):
                errors["base"] = "router_in_use"
            else:
                self.config["routers"] = [r for r in self.config["routers"] if r["id"] != rid]
                return await self.async_step_menu()
        if not self.config["routers"]:
            return await self.async_step_menu()
        return self.async_show_form(
            step_id="remove_router",
            data_schema=vol.Schema(
                {
                    vol.Required("router"): select([r["id"] for r in self.config["routers"]]),
                }
            ),
            errors=errors,
        )

    async def async_step_remove_channel(self, user_input=None):
        if user_input is not None:
            cid = user_input["channel"]
            self.config["channels"] = [c for c in self.config["channels"] if c["id"] != cid]
            self.config["settings"]["alert_channels"] = [
                c for c in self.config["settings"]["alert_channels"] if c != cid
            ]
            return await self.async_step_menu()
        if not self.config["channels"]:
            return await self.async_step_menu()
        return self.async_show_form(
            step_id="remove_channel",
            data_schema=vol.Schema(
                {
                    vol.Required("channel"): select([c["id"] for c in self.config["channels"]]),
                }
            ),
        )


class WanWatchConfigFlow(Wizard, config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            self.config = {
                "name": user_input["name"],
                "routers": [],
                "channels": [],
                "targets": copy.deepcopy(DEFAULT_TARGETS),
                "settings": copy.deepcopy(DEFAULT_SETTINGS),
                "egress_router": "",
            }
            self.config["settings"]["alert_channels"] = ["internet"]
            return await self.async_step_menu()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("name", default="Internet"): str,
                }
            ),
        )

    async def async_step_finish(self, user_input=None):
        if not self.config["routers"]:
            return await self.async_step_choose_router()
        if not self.config["channels"]:
            return await self.async_step_choose_channel()
        validate_topology(self.config["routers"], self.config["channels"])
        return self.async_create_entry(title=self.config["name"], data=self.config)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return WanWatchOptionsFlow()


class WanWatchOptionsFlow(Wizard, config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        self.config = copy.deepcopy(dict(self.config_entry.data))
        return await self.async_step_menu()

    async def async_step_finish(self, user_input=None):
        if not self.config["channels"]:
            return await self.async_step_choose_channel()
        validate_topology(self.config["routers"], self.config["channels"])
        self.hass.config_entries.async_update_entry(self.config_entry, data=self.config)
        return self.async_create_entry(title="", data={})
