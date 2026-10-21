"""Repair flow: replace a key that is about to expire."""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant

from .config_flow import KEY_SCHEMA, FlowError, async_validate


class ReplaceKeyFlow(RepairsFlow):
    """Asks for the new key, checks it reaches the same instance, then reloads."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_removed")
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_API_KEY: user_input[CONF_API_KEY]}
            try:
                instance = await async_validate(self.hass, data)
            except FlowError as err:
                errors["base"] = err.key
            else:
                if instance.instance_id != entry.unique_id:
                    errors["base"] = "wrong_instance"
                else:
                    self.hass.config_entries.async_update_entry(entry, data=data)
                    self.hass.config_entries.async_schedule_reload(entry.entry_id)
                    return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="confirm",
            data_schema=KEY_SCHEMA,
            description_placeholders={"entry": entry.title},
            errors=errors,
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    entry_id = str((data or {}).get("entry_id", ""))
    return ReplaceKeyFlow(entry_id)
