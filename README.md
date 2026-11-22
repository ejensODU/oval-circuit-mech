# Relay candidate for Builders Mech Royale

This Python bot builds on Relay commit `4e8597dcffbbf0acbd838814940c0fd9a85bbd4e`. The Dockerfile is at the repository root. The bot uses the Python standard library and reads the arena from its match mount.

## Changes

Relay considers a landed supply drop contested when a live rival occupies its ring near ground level. It can switch to a fight or another objective, then reconsider the drop when the ring clears. A stalled capture eventually times out even if the server keeps reporting Relay as the holder. The planner also applies its existing two-opponent ambush check before ranking drops, so a later fallback can’t select the rejected drop.

Startup calculates the ground clearance for the base mech once. For taller mechs, it propagates the additional obstacles from low overhead structures. The three resulting clearance maps matched Relay’s across all 774,680 campus grid cells. The navigation graph, connected components and landmark distances also matched. In four alternating native builds, arena initialization averaged 2.27 CPU seconds for the candidate and 3.92 for Relay.

## Verification

The organizer’s recorded stream passed in a one-CPU, 512 MB, read-only Docker container. The bot announced itself as Relay in 3.30 seconds and answered all 300 observations within 50 ms. The 99th percentile response time was 5.0 ms. The six closed-loop drills each passed on seeds 1, 2, 3, 117 and 421. Three additional tests cover an occupied drop, a fallback during an ambush, and a stalled capture clock.

The local squad simulator uses 15 synthetic opponents. The comparison uses paired spawns, the same squad configuration for both builds (skill 0.85, aggression 0.7 and sight sharing at 140 m), and seeds 8700–8711 and 9100–9111. The candidate tied Relay’s placement on all 24 seeds. Both scored 118 placement points (4.92 per match).

With the extracted organizer kit beside this directory, run these commands from the candidate root.

```sh
python3 -B ../mechfight-kit/check/check.py -- python3 -B -u bot.py
python3 -B tools/test_drop.py --kit ../mechfight-kit
python3 -B tools/spar.py --kit ../mechfight-kit --seeds 1,2,3,117,421 -- python3 -B -u bot.py
```

The candidate has no official practice score.
