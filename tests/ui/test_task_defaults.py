"""The task form's default values and added categories (app/ui/task_defaults.py), Tk-free."""

from __future__ import annotations

import pytest

from app.ui.task_defaults import TaskDefault, TaskDefaults, TaskDefaultsStore, parse_default
from app.ui.task_form_model import CATEGORIES


def test_built_in_defaults_are_named_after_their_category() -> None:
    defaults = TaskDefaults()
    assert defaults.for_category(None) == TaskDefault("Task", 60, 5, 20) == defaults.for_category("")
    assert defaults.for_category("study") == TaskDefault("Study", 60, 5, 20)
    assert defaults.all_categories() == CATEGORIES


def test_changes_and_added_categories_are_stored_and_reset(tmp_path) -> None:
    store = TaskDefaultsStore(tmp_path / "task_defaults.json")
    value = store.value.with_category("  Side   project ").with_default("study", parse_default("Revise", "1:30", "7", "40"))
    value = value.with_default(None, TaskDefault("Something", 15, 3, 0))
    assert store.save(value)

    reopened = TaskDefaultsStore(store.path).value
    assert reopened == value and reopened.all_categories() == [*CATEGORIES, "Side project"]
    assert reopened.for_category("study") == TaskDefault("Revise", 90, 7, 40)
    assert reopened.for_category("Side project").name == "Side project"
    assert reopened.for_category("") == TaskDefault("Something", 15, 3, 0)

    assert store.reset() and TaskDefaultsStore(store.path).value == TaskDefaults()


def test_invalid_input_is_refused() -> None:
    defaults = TaskDefaults().with_category("Garden")
    for name in ("", "study", "GARDEN", "x" * 41):
        with pytest.raises(ValueError):
            defaults.with_category(name)
    with pytest.raises(ValueError):
        defaults.without_category("study")  # only an added category can be removed
    assert defaults.with_default("Garden", TaskDefault("Weed")).without_category("Garden") == TaskDefaults()
    for values in (("", "60", "5", "20"), ("A", "soon", "5", "20"), ("A", "60", "11", "20"), ("A", "60", "5", "1001")):
        with pytest.raises(ValueError):
            parse_default(*values)


def test_a_damaged_file_falls_back_to_the_built_in_values(tmp_path) -> None:
    path = tmp_path / "task_defaults.json"
    path.write_text('{"general": {"name": "", "duration": 0}, "categories": {"study": 3}, '
                    '"custom_categories": ["Garden", 4, "study", "Garden"]}', encoding="utf-8")
    assert TaskDefaultsStore(path).value == TaskDefaults(custom_categories=("Garden",))
    path.write_text("not json", encoding="utf-8")
    assert TaskDefaultsStore(path).value == TaskDefaults()
