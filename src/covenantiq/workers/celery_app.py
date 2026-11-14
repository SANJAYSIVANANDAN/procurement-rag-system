"""The Celery application object, for `celery -A covenantiq.workers.celery_app`.

Importing this module builds the app from the environment, so only workers and the task modules
import it. Everything else uses `create_celery_app` from `app_factory`.
"""

from covenantiq.workers.app_factory import create_celery_app

celery_app = create_celery_app()

__all__ = ["celery_app", "create_celery_app"]
