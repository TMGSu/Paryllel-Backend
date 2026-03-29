from app.api.routes.users import router as users_router
from app.api.routes.uploads import router as uploads_router
from app.api.routes.communities import router as communities_router
from app.api.routes.posts import router as posts_router
from app.api.routes.comments import router as comments_router
from app.api.routes.search import router as search_router
from app.api.routes.widgets import router as widgets_router
from app.api.routes.moderation import router as moderation_router
from app.api.routes.admin import router as admin_router
from app.api.routes.tips import router as tips_router
from app.api.routes.admin_withdrawals import router as admin_withdrawals_router
from app.api.routes.stripe_webhook import router as stripe_webhook_router

all_routers = [
    users_router,
    uploads_router,
    communities_router,
    posts_router,
    comments_router,
    search_router,
    widgets_router,
    moderation_router,
    admin_router,
    tips_router,
    admin_withdrawals_router,
    stripe_webhook_router,
]