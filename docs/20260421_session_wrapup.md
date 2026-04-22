# Session Wrap-Up — 2026-04-21

## Shipped today (`version-zrjialumf3`)

- **Timeline click-to-modal** — timeline events are now clickable buttons that open a centered modal with:
  - Event kicker (date + type), title, description
  - **Linked trip card** — any trip whose `trip_date_start..trip_date_end` overlaps the event date ±30 days. Shows resort, dates, location, `cost_usd`, participant names, notes.
  - Related person (name, relationship, email, phone, notes)
  - Related property (name, developer, unit, week, network, city/country)
  - Related contact (name, role, org, email, phone, last contacted)
- **New endpoint** — `GET /timeshare/g/<uuid>/timeline/<pk>/detail` returns JSON `{event, person, property, contact, trips[]}`. Trips are fetched server-side via the date-overlap SQL so no client-side joining.
- **Admin-only deletes** stay intact on timeline rows (`{% if role in ('owner', 'admin') %}` wrapper around ✕ button is unchanged; click on the new button area just opens the modal).

## Reusable gotcha — modals must reparent to `<body>`

Any `position: fixed; inset-0` modal on crab.travel **must** do this on mount:

```js
if (modal && modal.parentElement !== document.body) document.body.appendChild(modal);
```

**Why:** the glass-nav base layout has an ancestor with `backdrop-filter: blur(...)` (and/or `transform`). Per CSS spec, either one creates a new containing block for `position: fixed` descendants, so `inset-0` resolves against the ancestor's box instead of the viewport. Symptom this session: `getBoundingClientRect()` returned `height: 8679px` (full content flow) instead of `900px` (viewport), and the inner dialog landed at `y: 4256` — far off-screen. Playwright reported the modal as "visible" because the element was in the DOM with non-zero size, but nothing a human could see.

**How to diagnose if it happens again:** dump computed styles + bounding rect of the modal element. If `position: fixed` is correct but `rect.height` is much larger than viewport height, something is trapping it. Reparent to `<body>` is the fix in every case.

Applies to every future modal here — resort drawer, new fact forms, confirmation dialogs, etc. Likely applies to any sibling project reusing the same glass-nav pattern (check `base.html` for `backdrop-blur` classes).

## Not touched

`next_steps.md` pending queue is unchanged — 6 timeshare items still queued (worth-it card, cycle seed button, searchbar autocomplete, NL dashboard search, weather/season filter, Drive watcher cron). Timeline modal came up mid-session and wasn't on the list, so nothing needs to be removed.
