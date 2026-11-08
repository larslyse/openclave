"""Load synthetic agent definitions before the rest of the unit suite imports app code."""
from pathlib import Path

import agents

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "Agents"
agents.AGENTS_ROOT = FIXTURE_ROOT
agents._load_created_agents()

# vault snapshots the configured path at import time; make the test process
# share the same synthetic data directory while individual tests patch it.
import vault
vault.AGENTS_ROOT = FIXTURE_ROOT
