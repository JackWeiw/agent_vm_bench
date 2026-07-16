"""
Scenario Configuration Loader Module

Loads scenario definitions from YAML file and provides prompt lookup by scenario name.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

import yaml


@dataclass
class ScenarioDefinition:
    """Single scenario definition"""

    prompt: str  # Initial prompt for this scenario


@dataclass
class ScenarioConfig:
    """Scenario configuration container"""

    scenarios: Dict[str, ScenarioDefinition] = field(default_factory=dict)
    default: str = ""

    def get_prompt(self, scenario_name: str) -> str:
        """Get prompt for a scenario"""
        if scenario_name in self.scenarios:
            return self.scenarios[scenario_name].prompt
        raise ValueError(f"Scenario '{scenario_name}' not found in configuration")

    def get_default_scenario(self) -> str:
        """Get default scenario name"""
        if self.default:
            return self.default
        if self.scenarios:
            return next(iter(self.scenarios.keys()))
        raise ValueError("No scenarios defined in configuration")

    def list_scenarios(self) -> list:
        """List all available scenario names"""
        return list(self.scenarios.keys())


def load_scenarios(config_path: str) -> ScenarioConfig:
    """
    Load scenario configuration from YAML file.

    Args:
        config_path: Path to scenarios.yaml file

    Returns:
        ScenarioConfig object

    Raises:
        FileNotFoundError: If config file doesn't exist
        ValueError: If config file is invalid
    """
    path = Path(config_path)

    if not path.exists():
        raise FileNotFoundError(f"Scenario config file not found: {config_path}")

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    scenarios = {}
    for name, definition in data.get("scenarios", {}).items():
        if isinstance(definition, dict) and "prompt" in definition:
            scenarios[name] = ScenarioDefinition(prompt=definition["prompt"])
        elif isinstance(definition, str):
            # Simple format: scenario_name: "prompt text"
            scenarios[name] = ScenarioDefinition(prompt=definition)

    default = data.get("default", "")

    return ScenarioConfig(scenarios=scenarios, default=default)


def find_scenario_file(llm_scenario_file: str = "") -> str:
    """
    Find scenario config file path.

    Args:
        llm_scenario_file: Explicit path from config (optional)

    Returns:
        Absolute path to scenarios.yaml
    """
    if llm_scenario_file:
        return os.path.abspath(llm_scenario_file)

    # Default location: llm_replay/config/scenarios.yaml
    # Find project root relative to this file
    this_dir = Path(__file__).parent
    default_path = this_dir.parent / "llm_replay" / "config" / "scenarios.yaml"

    if default_path.exists():
        return str(default_path)

    # Fallback: same directory as this module
    return str(this_dir / "scenarios.yaml")