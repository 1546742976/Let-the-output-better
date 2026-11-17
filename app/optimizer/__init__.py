"""对外入口：``optimize()``（README §7.1）。"""

from app.optimizer.pipeline import OptimizeResult, new_request_id, optimize

__all__ = ["optimize", "OptimizeResult", "new_request_id"]
