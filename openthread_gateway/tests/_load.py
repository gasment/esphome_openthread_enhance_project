"""Load the pure protocol without importing HA's integration entry point."""
import importlib
from pathlib import Path
import sys
import types

package = types.ModuleType("otgw_under_test")
package.__path__ = [str(Path(__file__).resolve().parents[2] / "custom_components" / "openthread_gateway")]
sys.modules.setdefault(package.__name__, package)
protocol = importlib.import_module("otgw_under_test.protocol")
hub = importlib.import_module("otgw_under_test.hub")
