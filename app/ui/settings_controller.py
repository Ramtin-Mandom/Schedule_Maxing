"""Persisted default preferences, independent of any selected date's overrides."""
from dataclasses import dataclass
from datetime import date

from app.planning.preferences import OptimizerMode, PreferenceOverrides, resolve_day_preferences
from app.ui import preferences_model as prefs
from app.ui.background import ControllerResult


@dataclass(frozen=True)
class DefaultPreferencesView:
    layer: PreferenceOverrides
    version: int | None
    engine: OptimizerMode
    inherited_engine: OptimizerMode
    rows: list[prefs.PreferenceRow]
    timezone: str


class TaskDataResetController:
    """
    Settings' "Reset All Task Data" (Tk-free): runs the services' reset and
    turns every failure into a message that says nothing was deleted. The
    services do the server part first and touch local data only after it
    succeeded (AppServices / DirectAppServices.reset_task_data).
    """

    def __init__(self, services) -> None:
        self._services = services

    def reset(self) -> ControllerResult:
        from app.persistence.errors import NotSignedInError
        from app.sync.transport import AuthenticationError, ProtocolError, TransportError

        try:
            return ControllerResult.success(self._services.reset_task_data())
        except TransportError as error:
            return ControllerResult.failure("The server could not be reached, so nothing was deleted -- not on the "
                                            "server and not on this device. Try again when you are online.", error)
        except (AuthenticationError, NotSignedInError) as error:
            return ControllerResult.failure("Your sign-in has expired. Sign in again on the Account page, then reset. "
                                            "Nothing was deleted.", error)
        except ProtocolError as error:
            return ControllerResult.failure(f"The server refused the reset: {error}. Nothing was deleted.", error)
        except RuntimeError as error:
            return ControllerResult.failure(f"{error} Nothing was deleted.", error)
        except Exception as error:  # noqa: BLE001 - the reset is transactional: a failure changed nothing
            return ControllerResult.failure(f"The reset failed and nothing was deleted: {error}", error)


class SettingsController:
    def __init__(self, planning, *, today=date.today):
        self.planning, self.today = planning, today

    def load(self):
        day = self.today()
        result = self.planning.preference_views(day, day)
        if not result.ok:
            return ControllerResult.failure(result.error, result.cause)
        views = result.value
        stored = views.user_layer
        layer = stored.overrides if stored else PreferenceOverrides()
        inherited = resolve_day_preferences(date=day, timezone=views.timezone_name, yaml_overrides=views.template)
        effective = views.days[day].inherited  # explicitly excludes the date layer
        categories = prefs.field_categories(effective.category_multipliers, effective.category_preferred_windows,
                                           layer.category_multipliers, layer.category_preferred_windows)
        rows = prefs.preference_rows(prefs.field_specs(effective.optimizer_mode, categories), effective=effective,
                                     inherited=inherited, layer=layer, layer_name="your defaults")
        return ControllerResult.success(DefaultPreferencesView(layer, stored.version if stored else None,
                                        effective.optimizer_mode, inherited.optimizer_mode, rows, views.timezone_name))

    def change(self, view, key, action, value=None):
        spec = next((row.spec for row in view.rows if row.spec.key == key), None)
        if spec is None:
            return ControllerResult.failure("That setting is unavailable for this engine. Reload the defaults.")
        try:
            change = {"save": lambda: prefs.with_value(view.layer, spec, value),
                      "inherit": lambda: prefs.inherit(view.layer, spec),
                      "clear": lambda: prefs.clear(view.layer, spec)}[action]()
        except (ValueError, KeyError) as error:
            return ControllerResult.failure(str(error), error)
        return self._save(view, change)

    def set_engine(self, view, mode):
        try:
            mode = OptimizerMode(mode) if mode is not None else None
        except ValueError as error:
            return ControllerResult.failure("Choose a supported engine.", error)
        return self._save(view, view.layer.model_copy(update={"optimizer_mode": mode}))

    def reset(self, view):
        return self._save(view, None)

    def _save(self, view, layer):
        result = self.planning.set_user_overrides(layer, expected_version=view.version)
        if not result.ok:
            return ControllerResult.failure(result.error, result.cause)
        return self.load()
