# Desktop shell, layout and design system (Milestone 4, Prompt 1)

The desktop app is Python/CustomTkinter only. It needs no browser, local HTTP
server, webview or Node build.

```bash
python -m app.app
```

It opens on **Day Schedule** with the sidebar collapsed.

## Navigation

| Page | What it is now |
| --- | --- |
| Day Schedule | Today's workspace ([desktop-day.md](desktop-day.md)): the Day Window, the horizontal timeline, available tasks, the task form, the Engine choice beside Make Schedule, Day Preferences, CSV, Reset Day, the task list and the **Execute** tab |
| Week / Month Schedule | Real calendar week and month ([desktop-calendar.md](desktop-calendar.md)): select a day, **Open Day**, the task form on the selected date, Reset Week/Month, the task list |
| Project Schedule / Allocation Planning | Persisted project CRUD, project tasks, date-only allocation previews and selected-date scheduling ([details](desktop-projects-allocation.md)) |
| Account | Registration/sign-in, explicit local-data association, profile, sync status and conflict resolution |
| How to Use | The in-app guide: what the app does and how every view and feature works (`app/ui/guide_page.py`, text in `app/ui/guide_content.py`) |
| About | Project purpose and creator attribution |
| Productivity | The existing productivity analytics (scrolls on small windows) |
| Settings | English, appearance, interface size, persisted default engine and active scheduling preferences ([details](desktop-settings.md)) |

- The sidebar starts collapsed and shows one letter per page (the page name
  appears as a tooltip on hover or focus).
- The ☰ button opens it with labels. It is reachable with Tab and works with
  Enter or Space. **Ctrl+B** also opens and closes it.
- **Ctrl+1 … Ctrl+9** jump to the first nine pages of the sidebar (Day … How to Use; About is reached from
  the sidebar). Choosing Day Schedule this way, or in the sidebar, shows today.
- The active page has a bar at its left edge as well as the highlight color.
- On a narrow window the sidebar closes after you pick a page. **Escape**
  also closes it there.
- Each schedule page keeps its own date while you move between pages. The
  shell also remembers it (`ShellState.remember`), so a rebuilt page can
  restore it.

## Responsive layout

Page width is measured in logical pixels (the window width divided by the
interface size).

| Mode | Width | Week / Month page | Day page |
| --- | --- | --- | --- |
| Wide | ≥ 1260 px | One scrolling column; below the calendar and the selected day, the task form beside the actions and task list | One scrolling column; below the timeline, the task form beside the actions and task list |
| Medium | 820–1259 px | As wide | As wide |
| Narrow | < 820 px | Everything stacked; the date field wraps under the buttons; the calendar scrolls sideways at its minimum column width | Everything stacked in one column; the header text wraps narrower |

- The active switcher button is marked with ✓, not only by color.
- Opening a task for editing brings the task input panel forward.
- A ±24 px band around each breakpoint keeps a window resized near the edge
  from flipping back and forth.
- Panels scroll internally, so no control is cut off on a small window.
- The window's minimum size (520×440) is only a sanity floor. The app does
  not rely on a large minimum size.

Why moving and resizing stay stable:

- Only the visible page is gridded; hidden pages are removed from layout. A
  resize lays out one page, not every page.
- The page host's `<Configure>` events are coalesced (`app/ui/layout.py`)
  into one width check after each burst.
- Pages are re-gridded only when the mode changes, never per pixel, and
  nothing is rebuilt.
- Nothing changes the window's own geometry, so no Configure feedback loop
  can form. A minimized window, which reports width 1, is ignored.
- Schedule drawing is coalesced, so moving or resizing never repaints the
  schedule.
- The sidebar animation has a fixed number of steps and stops if its widget
  is destroyed.

Measured on this machine: before the rebuild, one resize produced about 320
`<Configure>` events, and the content asked for 1818×1040 px inside a
1600×950 window, so it overflowed and was clipped. After the rebuild, one
resize produces about 50 events and the content fits its window.
`tests/ui/test_desktop_shell.py` checks that after every resize, move and
minimize/restore the layout goes quiet, the page fits, and the schedule is
not repainted.

## Appearance and interface size

- **Settings → Appearance**: Light or Dark, and interface size 90–130%, on
  top of the system DPI scaling.
- These choices are saved in `ui_settings.json` next to the database
  (`app/ui/ui_settings.py`).
- That file also stores the English language choice: no account data, no secrets, and no
  scheduling or reward settings.
- If the file is missing or damaged, the defaults are used; an invalid value
  falls back to its own default.

## Design system

Tokens live in `app/ui/theme.py`:

- Colors are (light, dark) pairs.
- Text sizes and spacing are defined in one place.
- Category colors are muted pastels. Fixed blocks are drawn in their real
  category's color.
- An unknown imported category gets a stable color derived from its name.
  The stored category is never renamed or remapped.
- Every text color is checked against its background for at least 4.5:1
  contrast in both appearances (`tests/ui/test_shell_foundation.py`).

Reusable widgets live in `app/ui/components.py`:

| Widget | Keyboard and accessibility behavior |
| --- | --- |
| `AppButton` (primary / secondary / neutral / danger / ghost) | Reachable with Tab, shows a focus ring, activates with Enter or Space |
| `LabeledEntry` | Visible label that focuses its field when clicked; errors appear as "Error: …" text |
| `LabeledSelect` | Up/Down/Home/End change the value; Enter, Space, Alt+Down or F4 open the list |
| `Card`, `SectionTitle` | — |
| `Notice` | Every status is written out ("Error:", "Warning:", "Info:", "Done:") |
| `StateView` | Loading, empty, and error states, with an optional action |
| `ContextMenu` | Opens with a right-click, the Menu key, or Shift+F10 |
| `ConfirmDialog`, `ChoiceDialog` | Escape cancels; Enter confirms, except for a destructive action, where Cancel is the default; focus starts inside and returns to where it was |
| `Drawer` | Slides over the page; Escape closes it and focus returns |
| `Tooltip` | — |

## Keyboard use of tasks

The Day timeline and the Week/Month calendars are keyboard-operable too
([desktop-day.md](desktop-day.md), [desktop-calendar.md](desktop-calendar.md)). On the Day timeline:

- Left/Right/Home/End select an item.
- **Enter** edits the selected item.
- **Delete** removes it.
- The Menu key or **Shift+F10** opens its actions: Edit and Remove.

The Uncompleted | Tasks | Completed board's × and → buttons and Week/Month's
All Tasks Complete / No Tasks Complete are reached with Tab and pressed with
Enter or Space.

## Background work

- Workers never touch Tk. Results are handed to the Tk thread and dropped if
  the target widget is gone or shutdown has started.
- Since this step, each result is also dropped if the account or workspace
  changed while the work ran. The worker registry's default guard is
  `AppServices.workspace_guard`.
