---
title: Spoolman support
---

`moonraker.custom.conf`

```
[spoolman]
server: https://spoolman.domain
sync_rate: 5
```

Macros are registered automatically by Rinkhals:
- M555
- SET_ACTIVE_SPOOL
- CLEAR_ACTIVE_SPOOL

## Single filament Spoolman support

Per filament in OrcaSlicer's filament start gcode:

```gcode
; filament start gcode
SET_ACTIVE_SPOOL ID=5
```

Optionally clear spool tracking in OrcaSlicer's filament end gcode:

```gcode
; filament end gcode
CLEAR_ACTIVE_SPOOL
```

## ACE gate → Spoolman integration (`[mmu_ace]`)

If your printer has an ACE unit, `[mmu_ace]` options control how gate filament data is handled.

`moonraker.custom.conf`

```ini
[mmu_ace]
spoolman_support: push
printer_name: My Printer
```

Every gate is resolved the same way, every poll, in this order:

1. **A local manual/Spoolman link**, if one exists for that gate — always wins, even on a gate
   that currently has a physical RFID tag. Editing a gate (material, name, color, temperature,
   and optionally a Spoolman spool ID) is always allowed; linking a real Spoolman spool ID pulls
   that spool's actual filament name, material, color, and temperature from Spoolman
   automatically, overwriting whatever was typed in locally. Every edit persists
   (`mmu_ace_gate_spools.json`), even a bare edit with no spool ID linked.

   If the gate has a physical tag and that tag later reports a *different* SKU (the physical
   spool was swapped without updating the gate mapping), the link auto-clears and RFID takes
   back over. This only detects a SKU change — two different rolls of the identical SKU swapped
   for each other can't be told apart, since Anycubic's tags carry a SKU code, not a per-roll
   unique ID.
2. **A Spoolman-pulled assignment** (only under `spoolman_support: pull`, and only for gates with
   no local link) — see below.
3. Otherwise, the gate's own RFID tag data, or "Unknown"/empty if it has no tag.

A gate's Spool ID is never derived from its RFID tag's SKU — an untouched, tagged-but-unlinked
gate always reports no spool ID, so it's never mistaken for (or pushed to) an unrelated real
Spoolman spool that happens to share the same number.

### `spoolman_support`: `off` | `push` | `pull` (default: `off`)

- **`off`** (default) — ACE gate loads never touch Spoolman. Single filament Spoolman support
  above still works, and gates can still be manually linked/edited locally.
- **`push`** — whenever the loaded gate changes (a filament swap, endless-spool handoff, etc.),
  Rinkhals sets Spoolman's active spool automatically (the same call path as `SET_ACTIVE_SPOOL`,
  just triggered by the hardware instead of a gcode line). Linking a gate to a spool ID also
  writes that gate's assignment out to Spoolman itself, so it's visible from Spoolman's own UI too.
- **`pull`** — everything `push` does, plus Rinkhals periodically imports gate assignments *from*
  Spoolman (checked on the same interval as `[spoolman] sync_rate`) and treats them as
  authoritative for any gate that doesn't already have a local link.

Gate-assignment sync with Spoolman (`push`/`pull`) needs Spoolman 0.18.1 or later — it uses
Spoolman's own "extra fields" feature (`printer_name`, `mmu_gate_map`) to record which gate a
spool is in, plus Spoolman's built-in `location` field for a human-readable summary. This is
separate from, and in addition to, setting the active spool for usage tracking.

### `printer_name` (default: unset)

Only matters for `push`/`pull`. Spoolman is commonly a single shared server across more than one
printer, so gate assignments are scoped by this name — if it's unset, Rinkhals falls back to your
printer's configured name in Fluidd (Settings → General), then to your OS hostname as a last
resort. Set it explicitly if you have more than one printer sharing the same Spoolman instance
and haven't already given each one a distinct name in Fluidd. Rinkhals warns at startup if
resolution falls all the way through to the OS hostname and that hostname is the generic one
stock Rinkhals images all share — in that situation, gate assignments can silently collide
between printers.