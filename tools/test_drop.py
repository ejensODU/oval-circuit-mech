"""Exercise contested and stalled supply drops against the specified bot entry."""
import argparse
import gzip
import json
import sys
import tempfile
from pathlib import Path


def run(bot, drill):
    obs = drill.observe()
    drop = obs["drops"][0]
    x, y, z = drop["pos"]
    obs["self"]["pos"] = [x, y, z]
    obs["self"]["hp"] = obs["self"]["max_hp"]
    drop.update(holder=obs["self"]["id"], progress=1.0)

    def rival(actor_id, offset, visible=True):
        return dict(drill.enemy, id=actor_id, pos=[x + offset, y, z], _open=visible)

    def plan(contacts):
        obs["mechs"] = contacts
        return bot.plan(obs, contacts, bot.resources(obs, contacts))

    inside = plan([rival(7, drop["radius"])])
    outside = plan([rival(7, drop["radius"] + 0.1)])
    overhead = rival(7, 0)
    overhead["pos"][2] += 18.
    above = plan([overhead])
    yield "occupied vs clear ring", inside.mode != "capture" and outside.mode == above.mode == "capture"

    # Force the Finals fallback. The primary target is behind cover and the
    # two open rivals are close, while all three are outside the capture ring.
    bot.read.is_ffa = True
    ambush = plan([rival(7, 40, False), rival(8, 30), rival(9, 35)])
    drop["progress"] = drop["capture_s"] - 0.5
    near_finish = plan(obs["mechs"])
    yield "ambush fallback and near finish", ambush.mode != "capture" and near_finish.mode == "capture"

    # An unseen rival can freeze the clock without changing the holder.
    capture = plan([])
    for t, progress in [(1.0, 0.5), (12.0, 1.0), (20.0, 1.0)]:
        obs["t"], drop["progress"] = t, progress
        bot.update_progress(capture, obs)
    kept_after_advance = drop["id"] not in bot.blocked_until
    obs["t"] = 25.0
    bot.update_progress(capture, obs)
    yield "advancing vs frozen clock", kept_after_advance and bot.blocked_until.get(drop["id"], 0) > obs["t"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--kit", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.entry.resolve()))
    from bot import Bot
    from tools.spar import Drill, fixture

    with gzip.open(args.kit / "check/stream.jsonl.gz", "rt") as stream:
        hello, first = json.loads(next(stream)), json.loads(next(stream))
    with tempfile.TemporaryDirectory() as directory:
        mapdir = Path(directory) / "arena"
        fixture(mapdir, None, args.kit)
        hello["map"]["dir"] = str(mapdir)
        results = list(run(Bot(hello), Drill("drop", 1, hello, first)))
    for name, passed in results:
        print(f"{'PASS' if passed else 'FAIL'}  {name}")
    return 0 if all(passed for _, passed in results) else 1


if __name__ == "__main__":
    sys.exit(main())
