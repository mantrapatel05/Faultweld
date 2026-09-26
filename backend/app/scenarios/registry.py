from __future__ import annotations

from pathlib import Path

from app.scenarios.models import ScenarioDefinition

FIXTURES_DIR = Path(__file__).resolve().parents[4] / "scenarios" / "fixtures"


def load_scenarios(fixtures_dir: Path = FIXTURES_DIR) -> dict[str, ScenarioDefinition]:
    scenarios: dict[str, ScenarioDefinition] = {}
    for fixture_path in sorted(fixtures_dir.glob("*.json")):
        scenario = ScenarioDefinition.model_validate_json(fixture_path.read_text(encoding="utf-8"))
        if scenario.scenario_id in scenarios:
            raise ValueError(f"duplicate scenario ID: {scenario.scenario_id}")
        scenarios[scenario.scenario_id] = scenario
    return scenarios
