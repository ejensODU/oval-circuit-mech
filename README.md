# Relay for Builders Mech Royale

Relay opens with the Oval Circuit squad controller from commit `18efde548525`, which scored 5.54 in official practice. When the match observations indicate rival mechs are eliminating or damaging each other, it switches to Relay for free-for-all play. The switch is permanent for that match. Both controllers use the same precomputed arena. Relay keeps its observation history throughout the match.

The previous Relay commit `adbd424d9350` scored 5.50 in official practice. In the local 15-opponent squad simulator, the combined entry tied that version at 63 placement points across seeds 8700–8711 and scored 57 against its 55 across seeds 9100–9111. These synthetic matches use a different house squad from the organizer.

The combined entry passed the organizer’s checker in a one-CPU, 512 MB, read-only Docker container. It returned all 300 actions within the 50 ms budget. Startup took 4.74 seconds and the 99th percentile action time was 5.5 ms. A separate transition check verified that each observation updates the classifier once and that the free-for-all switch stays active.

## Run locally

Keep the organizer’s extracted kit beside this directory. Run the following from the repository root.

```sh
python3 -B ../mechfight-kit/check/check.py -- python3 -B -u bot.py
python3 -B tools/test_drop.py --kit ../mechfight-kit
```
