# Oval Circuit

A Python entry for the Builders Mech Royale track. Each decision uses the current observation, the match configuration and the supplied campus map. The bot runs with the Python standard library. The Dockerfile pins the base image by digest.

## Run the organizer's check

Unzip the project into an empty directory. Put the organizer's extracted `mechfight-kit` directory beside it. If using the original attachment, first expand `Builders-MechRoyale.tar.gz`, then expand the `mechfight-kit.tar.gz` inside it. The commands below run from the directory containing this README and the Dockerfile.

```sh
KIT=../mechfight-kit
python3 "$KIT/check/check.py" -- python3 -B -u bot.py
docker build --platform linux/amd64 -t oval-circuit .
ARENA_DIR="$(cd "$KIT/arena" && pwd)"
python3 "$KIT/check/check.py" --map-dir /arena -- \
  docker run --rm -i --platform linux/amd64 --network none --cpus 1 \
    --memory 512m --memory-swap 512m --read-only --pids-limit 128 \
    --cap-drop ALL --tmpfs /tmp:rw,size=64m --user 65534:65534 \
    -v "$ARENA_DIR:/arena:ro" oval-circuit
```

The checker feeds back a recorded stream, so its timing and protocol results cannot estimate match placement. The image builds and passes the check under the match sandbox flags above: 300 of 300 answers on time, about 62 MB of memory, and about 6 seconds from `hello` to `ready`.

[Track rules and leaderboard](https://www.joinbuilders.org/buildathon/tracks/builders) · [Buildathon rules](https://www.joinbuilders.org/buildathon)

## How it plays

- **Positioning.** At startup the bot reads terrain, solid prisms, bridges and roof heights. It builds a graph of traversable ground with clearance for its body. Landmarks help estimate travel time around buildings. The controller reserves energy for ledges and roof approaches and anticipates the next safe circle.
- **Fighting.** The controller leads the arm gun, checks the torso's actual aim before releasing the laser, evaluates curved mortar paths, and fires lightning in range. The legs can move across an opponent's view while the torso tracks them. Incoming arena projectiles influence the next movement direction.
- **Survival.** The bot farms nearby cubes and weapons, values a supply drop for its repair as well as its cubes, and seeks shelter when several opponents focus it. An injured mech can continue shooting while escaping, then pause to regenerate behind cover. A health loss and another enemy's aim can signal a fight between rivals.
- **Late game.** An equipped mech can hold shelter in the next circle and pass up distant loot. If the closing circle lies on a building, the bot looks for walkable ground inside it and can climb when the roof is the safe route.

## Local drills and measured results

Run the same changing game states against either implementation. The harness uses the kit's configuration, a simplified 20 Hz game loop and six fixed synthetic layouts or scenarios. Each bot runs as a subprocess and receives observations generated from its previous actions. The seeds vary starting conditions, projectile spread and opponent movement phase. These drills do not contain the organizer's house bots, complete physics, or a 16-player tournament.

```sh
KIT=../mechfight-kit
SEEDS=1,2,3,117,421,897,1502,2493,3119,4527,5962
python3 tools/spar.py --kit "$KIT" --seeds "$SEEDS" -- python3 -B -u bot.py
python3 tools/spar.py --kit "$KIT" --seeds "$SEEDS" -- python3 -B -u "$KIT/python/example_bot.py"
```

| Outcome in 11 paired seeds | Oval Circuit | Kit Python example |
| --- | ---: | ---: |
| Defeated the strafing gun opponent before the duel timer | 11/11 | 0/11 |
| Took the laser, opened the crate and collected both cubes | 11/11 | 0/11 |
| Finished inside the safe circle beyond a tall wall | 11/11 | 0/11 |
| Finished inside the closing circle on an 18 m roof | 11/11 | 0/11 |
| Completed the repair drop under approaching fire | 11/11 | 0/11 |
| Finished the wounded target with a laser while the gun reloaded | 11/11 at 0.45 s | 11/11 at 2.75 s |

The entrant spent zero seconds outside the circle across the 22 wall and roof runs. The beam drill uses a target with 180 HP. A partial laser charge is enough to finish it. The results describe these fixtures only. Official practice and tournament placements remain unmeasured.

## Source and tuning notes

`mechfight.py` is the transport wrapper supplied in the organizer's Python starter kit. The strategy, navigation, combat, movement and drill implementations were written for this entry with AI assistance. The project uses no external Python packages. The official map, checker, schema and recorded match come from the separately supplied kit. The entry's license is in `LICENSE`.

| File | Behavior to change |
| --- | --- |
| `bot.py` | Timing of zone rotations, when to take fights, loot valuation, repair drops, upgrades |
| `tactics.py` | Preferred duel distance, movement across incoming fire, boost reserve, jet ascent |
| `combat.py` | Fire control, target selection, simultaneous weapon use |
| `arena.py` | Map geometry, pathfinding and shelter selection |
| `tools/spar.py` | Reproducible local behavior checks |
