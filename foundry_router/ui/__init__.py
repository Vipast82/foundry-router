from .routes import router  # noqa: F401
from .setup_routes import router as _setup_router

router.include_router(_setup_router)
