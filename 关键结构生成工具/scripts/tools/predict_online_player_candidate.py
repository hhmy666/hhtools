"""Print the average seven-slot win rate for a generated candidate file."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from puzzle_core.player_candidate_rollout import load_player_model, predict_player_candidate  # noqa: E402
from puzzle_core.template_reader import parse_level_file  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path, help="实际生成的候选 .bytes 文件")
    parser.add_argument("model", type=Path, help="真实玩家行为模型 JSON")
    parser.add_argument("--players", type=int, default=1200)
    parser.add_argument("--scenarios", type=int, default=10)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    try:
        level = parse_level_file(args.candidate)
        _, policies = load_player_model(args.model)
        result = predict_player_candidate(
            level, policies, scenario_count=args.scenarios,
            player_count=args.players, seed=args.seed,
        )
    except (OSError, ValueError, OverflowError) as exc:
        parser.error(str(exc))
    print(f"{result.win_rate:.1%}")


if __name__ == "__main__":
    main()
