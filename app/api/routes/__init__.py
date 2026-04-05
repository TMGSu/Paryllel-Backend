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
from app.api.routes.webhook import router as stripe_webhook_router
from app.api.routes.payouts import router as payouts_router
from app.api.routes.feeds.home import router as home_feed_router
from app.api.routes.feeds.community import router as community_feed_router
from app.api.routes.feeds.popular import router as popular_feed_router
from app.api.routes.feeds.explore import router as explore_feed_router
from app.api.routes.notifications import router as notifications_router


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
    payouts_router,
    stripe_webhook_router,
    home_feed_router,
    community_feed_router,
    popular_feed_router,
    explore_feed_router,
    notifications_router,
]