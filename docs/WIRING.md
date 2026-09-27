# Foyer wiring: Optex Accurance OV-102 -> RJL Pi

Source: Optex OV-102 datasheet (FROM SOURCE). Physical terminal numbers are NOT in the datasheet -
read them off the OV-102CB(E) control box on the bench and fill in the blanks below.

## Principle
The Pi only **listens** to the OV-102's volt-free relay outputs (MOSFET, 30 V DC 0.2 A max, resistive).
It never sends a signal into the Optex or the building's access control.

## Outputs -> Pi (via opto-isolated input board, 12 or 24 V loop, ~10 mA)
| OV-102 output | CB terminal | Dipswitch | Timer | Pi input (BCM) | Config name |
|---|---|---|---|---|---|
| Tailgating 1 | ____ | N.O. | ~3 s (NOT infinity) | 5 | optex_tailgating_1 |
| Multiple detections | ____ | N.O. + Multiple detections ON | - | 6 | optex_multiple |
| Error | ____ | **N.C.** (cut cable = alarm) | - | 13 | optex_error |
| Number of pass | ____ | N.O. | pulse | 19 | optex_number_of_pass |
| **Unlock command** | ____ | - | - | **NOT WIRED** | - |
| Tailgating 2, Normal entry, Authorization number | ____ | - | - | not wired (spare) | - |

`activeLevel` in the config is the GPIO level when the Optex output is **asserted**. It depends on the opto
board (most pull the GPIO LOW when the input is energised). Confirm on the bench and set per input.

Why the tailgate timer must not be "infinity": a latched output only clears through the Optex *Output reset*
input, which would require the Pi to drive a signal. Timed output clears itself.

## Inputs -> OV-102 (not from the Pi)
| OV-102 input | Source |
|---|---|
| Authorization | Preferred: volt-free "access granted" output programmed by Sky Garden's access-control integrator. Alternative: parallel Wiegand tap (only if the reader link is Wiegand, not OSDP) |
| Door open | Optex supplied magnet switch |
| Door locked / Disable output / Output reset | Unused |

## Power and network
Both OV-102 units are PoE 802.3af, 10 W each: a PoE switch (~20 W budget) is required; the control box does not
power the detection unit. Pi 5 + 4G on the same switch. Laptop needed at commissioning for the Optex web setup.

## Bench test (must pass before site)
1. PoE switch on, OV-102 warm-up ~45 s, Pi agent running with the real config.
2. Walk one person under the sensor: `PASS` event in the audit log and portal.
3. Two people close together without authorisation: `TAILGATE ASSERT` then `CLEAR` ~3 s later.
4. Two people standing in the zone: `MULTIPLE ASSERT`.
5. Cover the lens / kill the light: `SENSOR_ERROR ASSERT`.
6. Unplug the Error cable at the Pi end: `SENSOR_ERROR ASSERT` (proves N.C. supervision).
7. Pull the Pi's network for 10 min: events queue (`queueDepth` in heartbeat), RJL gets "unit offline",
   then all queued events arrive once reconnected, followed by "back online".
Record each result. Until done, hardware operation is UNVERIFIED.

## Site limits to confirm (datasheet)
Detection unit interior only, ceiling-mounted on the **non-secured (approach) side**, 2.5-4.0 m high; max door
opening 2 m at 2.5 m; manual swing or automatic sliding door only; >= 100 lux at all hours.
